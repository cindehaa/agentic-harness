"""The trial runner: one attempt at one task, start to finish (`ideation/agents-and-runner.md`)."""

import json
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

from agentic_harness.agents.base import Agent, AgentLimitation, AgentSession, Unsupported
from agentic_harness.benchmarks.base import Benchmark, GraderError, GraderSetupError
from agentic_harness.environments.base import Environment, EnvironmentFactory, EnvironmentLost
from agentic_harness.redact import redact, redact_file
from agentic_harness.types import Limits, Message, Outcome, Score, Task

GRADE_MARGIN_S = 600  # the container outlives the trial deadline by this much, so grading has time to finish


class TurnLimit(Exception):
    """The benchmark tried to send more than limits.max_turns messages."""


class RecordingSession(AgentSession):
    """What the benchmark is handed: forwards to the agent's session, records both sides, and caps the turns.

    Each event is appended to events.jsonl (redacted) as it happens, so a crash loses nothing already recorded;
    `on_turn_end` runs after every turn (the worker loads the file into the events table then)."""

    def __init__(self, inner: AgentSession, max_turns: int, events_file: Path, on_turn_end: Callable[[], None]) -> None:
        self.inner = inner
        self.max_turns = max_turns
        self.events_file = events_file
        self.on_turn_end = on_turn_end
        self.turns = 0
        self.seq = 0
        self.transcript: list[Message] = []

    def send(self, message: Message) -> Message:
        if self.turns >= self.max_turns:
            raise TurnLimit(f"more than {self.max_turns} turns")
        self.turns += 1
        self._record("message_sent", message)
        reply = self.inner.send(message)
        self._record("message_received", reply)
        self.on_turn_end()
        return reply

    def _record(self, kind: str, message: Message) -> None:
        self.transcript.append(message)
        event = {"seq": self.seq, "turn": self.turns, "kind": kind, "payload": asdict(message), "ts": time.time()}
        with self.events_file.open("a") as f:
            f.write(redact(json.dumps(event, default=str)) + "\n")
        self.seq += 1


def run_trial(
    benchmark: Benchmark, agent: Agent, task: Task, limits: Limits, attempt_dir: Path, env_factory: EnvironmentFactory,
    on_turn_end: Callable[[], None] = lambda: None,
) -> Outcome:
    """Run one attempt in a world from `env_factory`: events.jsonl as it goes, then outcome.json and transcript.json,
    all in attempt_dir, all redacted. Never raises (except KeyboardInterrupt): every failure becomes an Outcome."""
    attempt_dir.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    session: RecordingSession | None = None
    try:
        with env_factory(task.image, int(limits.trial_timeout_s + GRADE_MARGIN_S), limits) as env:
            inner = agent.start(env, attempt_dir, limits)
            session = RecordingSession(inner, limits.max_turns, attempt_dir / "events.jsonl", on_turn_end)
            outcome = _agent_turns(benchmark, task, env, session)
            if outcome is None:
                outcome = _grade(benchmark, task, env, session)
    except EnvironmentLost as e:
        outcome = Outcome("infra_error", error=f"{type(e).__name__}: {e}")
    except Unsupported as e:
        outcome = Outcome("unsupported", error=str(e))
    except Exception:
        outcome = Outcome("infra_error", error=traceback.format_exc())  # a harness or agent-adapter crash
    if session is not None:
        outcome = replace(
            outcome, agent_exit_status=session.inner.exit_status, cost_usd=session.inner.cost_usd, turns=session.turns
        )
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
        session.inner.exit_status = session.inner.exit_status or f"AgentLimitation: {e}"
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
    if state is None or state.oom_killed:  # gone, Docker unreachable, or out of memory (no limit of ours yet)
        return Outcome("infra_error", error=f"container lost: {state}")
    if not state.running:  # something inside killed the main process: the agent's doing
        score = Score(False, "container died during agent turn", {"state": state})
        return Outcome("graded", score, benchmark.score_to_float(score))
    return None


def _grade(benchmark: Benchmark, task: Task, env: Environment, session: RecordingSession) -> Outcome:
    try:
        env.kill_agent_processes()  # nothing the agent started may touch the world while it is graded
        score = benchmark.grade(task, env, tuple(session.transcript))
    except GraderSetupError as e:  # e.g. no network to install the grader's deps: infra, retried
        return Outcome("infra_error", error=f"GraderSetupError: {e}")
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


def _write(path: Path, data) -> None:
    """Redacted and atomic: a reader (or a crash) never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(redact(json.dumps(data, indent=2, default=str)))  # default=str: a stray object never blocks the write
    os.replace(tmp, path)
