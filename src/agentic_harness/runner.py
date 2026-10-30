"""The trial runner: one attempt at one task, start to finish (`ideation/agents-and-runner.md`)."""

import json
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

from agentic_harness.agents.base import Agent, AgentLimitation, TransientError, Unsupported
from agentic_harness.benchmarks.base import Benchmark, GraderError, GraderSetupError
from agentic_harness.environments.base import Environment, EnvironmentFactory, EnvironmentLost
from agentic_harness.recording import EventLog, RecordingEnvironment, RecordingSession, TurnLimit
from agentic_harness.redact import redact, redact_file
from agentic_harness.types import Limits, Outcome, Score, Task

# The container's keep-alive runs this long: generous, so it never ends before the trial does (if it does, that is
# infra_error, not the agent's doing). It is still a self-destruct timer if the harness dies.
LIFETIME_FACTOR = 3  # times the trial timeout
GRADE_MARGIN_S = 600  # plus time for grading to finish
WORKSPACE_MAX_BYTES = 50_000_000  # the saved final workspace (workspace.tar) is cut off here


def run_trial(
    benchmark: Benchmark, agent: Agent, task: Task, limits: Limits, attempt_dir: Path, env_factory: EnvironmentFactory,
    on_turn_end: Callable[[], None] = lambda: None,
) -> Outcome:
    """Run one attempt in a world from `env_factory`: events.jsonl as it goes, then outcome.json and transcript.json,
    all in attempt_dir, all redacted. Never raises (except KeyboardInterrupt): every failure becomes an Outcome.

    The agent gets the world wrapped in a RecordingEnvironment (its commands are traced); the benchmark gets it bare."""
    attempt_dir.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    session: RecordingSession | None = None
    try:
        with env_factory(task.image, int(LIFETIME_FACTOR * limits.trial_timeout_s + GRADE_MARGIN_S), limits) as env:
            log = EventLog(attempt_dir / "events.jsonl")
            inner = agent.start(RecordingEnvironment(env, log), attempt_dir, limits)
            session = RecordingSession(inner, limits.max_turns, log, on_turn_end)
            outcome = _agent_turns(benchmark, task, env, session)
            if outcome is None:
                outcome = _grade(benchmark, task, env, session)
    except EnvironmentLost as e:
        outcome = Outcome("infra_error", error=f"{type(e).__name__}: {e}", retryable=True)
    except Unsupported as e:
        outcome = Outcome("unsupported", error=str(e))
    except (TransientError, ConnectionError, TimeoutError):  # provider overloaded or rate limited, network down
        outcome = Outcome("infra_error", error=traceback.format_exc(), retryable=True)
    except Exception:  # auth, bad request, a harness or adapter bug: another attempt would fail the same way
        outcome = Outcome("infra_error", error=traceback.format_exc())
    if session is not None:
        stats = session.stats()
        exit_status = stats.exit_status or session.limitation
        outcome = replace(outcome, agent_exit_status=exit_status, cost_usd=stats.cost_usd, turns=session.turns)
    outcome = replace(outcome, duration_s=round(time.monotonic() - began, 3), error=outcome.error and redact(outcome.error))
    for path in attempt_dir.rglob("*"):  # files the agent wrote itself, e.g. mini's trajectory.json
        if path.is_file():
            redact_file(path)
    _write(attempt_dir / "outcome.json", asdict(outcome))
    _write(attempt_dir / "transcript.json", [asdict(m) for m in session.transcript] if session else [])
    return outcome


def _agent_turns(benchmark: Benchmark, task: Task, env: Environment, session: RecordingSession) -> Outcome | None:
    """Let the benchmark drive the agent. Returns an Outcome only if the container died meanwhile (rule 6)."""
    lost = None
    try:
        benchmark.run_agent(task, session, env)
    except TurnLimit:
        pass  # the agent's turns are over; grade what it did
    except AgentLimitation as e:  # the agent can't go on (context full, model refused): grade what it did, like a timeout
        session.limitation = f"AgentLimitation: {e}"
    except EnvironmentLost as e:
        lost = e  # maybe the agent killed its own container: the evidence below decides
    if died := _container_death(benchmark, env):
        return died
    if lost:
        raise lost
    return None


def _container_death(benchmark: Benchmark, env: Environment) -> Outcome | None:
    """Rule 6, from env.state(): None if the container still runs, else how the trial ends."""
    state = env.state()
    if state is None:  # gone, or Docker unreachable
        return Outcome("infra_error", error="container lost: gone, or Docker unreachable", retryable=True)
    if state.running:
        return None
    if state.exit_code == 0 and not state.oom_killed:  # the keep-alive ended on its own: its lifetime ran out, ours
        return Outcome("infra_error", error="container lifetime expired", retryable=True)
    # killed from inside, or over the memory limit we set (Limits.memory): the agent's doing
    why = "container ran out of memory during agent turn" if state.oom_killed else "container died during agent turn"
    score = Score(False, why, {"state": asdict(state)})
    return Outcome("graded", score, 0.0)  # decided by the harness, not the grader: 0 whatever the benchmark's scale


def _grade(benchmark: Benchmark, task: Task, env: Environment, session: RecordingSession) -> Outcome:
    try:
        env.kill_agent_processes()  # nothing the agent started may touch the world while it is graded
        _save_workspace(env, session)  # the agent's end state, before the grader adds anything
        score = benchmark.grade(task, env, tuple(session.transcript))
    except GraderSetupError as e:  # e.g. no network to install the grader's deps: infra, retried
        return Outcome("infra_error", error=f"GraderSetupError: {e}", retryable=True)
    except GraderError as e:
        return Outcome("grader_error", error=str(e))
    except EnvironmentLost:
        # The agent may have killed PID 1 as its last act and the container exited only now: rule 6 again
        if died := _container_death(benchmark, env):
            return died
        raise  # the container still runs: an infra problem
    try:
        json.dumps(score.value)  # the stored score must be JSON
        return Outcome("graded", score, benchmark.score_to_float(score))
    except (TypeError, ValueError) as e:
        return Outcome("grader_error", error=f"unusable score value {score.value!r}: {e}")


def _save_workspace(env: Environment, session: RecordingSession) -> None:
    """workspace.tar in the attempt folder (integrity evidence, `fidelity-vs-integrity.md`), noted in the trace.
    A workspace that can't be read (e.g. the agent deleted it) is noted, not fatal: the grader decides."""
    path = session.log.path.parent / "workspace.tar"
    try:
        truncated = env.save_workspace(path, WORKSPACE_MAX_BYTES)
    except OSError as e:
        session.log.write("workspace_saved", {"error": str(e)})
        return
    session.log.write("workspace_saved", {"file": path.name, "bytes": path.stat().st_size, "truncated": truncated})


def _write(path: Path, data) -> None:
    """Redacted and atomic: a reader (or a crash) never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(redact(json.dumps(data, indent=2, default=str)))  # default=str: a stray object never blocks the write
    os.replace(tmp, path)
