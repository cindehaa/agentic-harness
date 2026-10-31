import json
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

from agentic_harness.agents.base import Agent, AgentLimitation, AgentStats, TransientError, Unsupported
from agentic_harness.benchmarks.base import Benchmark, GraderError
from agentic_harness.environments.base import (
    EndReason, Environment, EnvironmentFactory, EnvironmentLost, EnvironmentState, ExecResult, Sidecar,
)
from agentic_harness.recording import EventLog, RecordingEnvironment, RecordingSession, TurnLimit
from agentic_harness.redact import add_forwarded, redact, redact_file
from agentic_harness.types import LimitOverrides, Limits, Outcome, Score, Task, effective_limits

LIFETIME_FACTOR = 3
MARGIN_S = 600
WORKSPACE_MAX_BYTES = 50_000_000


def run_trial(
    benchmark: Benchmark, agent: Agent, task: Task, defaults: Limits, attempt_dir: Path, env_factory: EnvironmentFactory,
    on_turn_end: Callable[[AgentStats], None] = lambda stats: None, flags: LimitOverrides = LimitOverrides(),
) -> Outcome:
    """Never raises (except KeyboardInterrupt): every failure becomes an Outcome."""
    attempt_dir.mkdir(parents=True, exist_ok=True)
    add_forwarded(agent.forward_env)  # before the agent can print them
    began = time.monotonic()
    session: RecordingSession | None = None
    phases: dict[str, float] = {}
    limits, sources = defaults, {}
    try:
        limits, sources = effective_limits(defaults, benchmark.limits(task), flags)
        if missing := agent.capabilities.missing(benchmark.requirements(task)):
            raise Unsupported(f"{agent.name} can't do what this task needs: {', '.join(missing)}")
        lifetime_s = int(LIFETIME_FACTOR * limits.trial_timeout_s + limits.grade_timeout_s + MARGIN_S)
        with env_factory(agent.prepare_image(task.image), lifetime_s, limits, task.workdir) as env:
            log = EventLog(attempt_dir / "events.jsonl")
            servers = tuple(benchmark.mcp_servers(task))
            for s in servers:
                env.attach(Sidecar(s.image, s.command, s.port, shares_world=False)).start()
            phases["env_ready"] = phases["agent_start"] = time.time()
            inner = agent.start(RecordingEnvironment(env, log), attempt_dir, limits, mcp_servers=servers)
            session = RecordingSession(inner, limits.max_turns, log, on_turn_end)
            outcome = _agent_turns(benchmark, task, env, session, phases)
            if outcome is None:
                outcome = _grade(benchmark, task, env, session, phases, limits.grade_timeout_s)
    except EnvironmentLost as e:
        outcome = Outcome("infra_error", error=f"{type(e).__name__}: {e}", retryable=True)
    except Unsupported as e:
        outcome = Outcome("unsupported", error=str(e))
    except (TransientError, ConnectionError, TimeoutError):
        outcome = Outcome("infra_error", error=traceback.format_exc(), retryable=True)
    except Exception:
        outcome = Outcome("infra_error", error=traceback.format_exc())
    if session is not None:
        stats = session.stats()
        outcome = replace(outcome, agent_exit_status=stats.exit_status or session.limitation, cost_usd=stats.cost_usd,
                          model_calls=stats.model_calls, input_tokens=stats.input_tokens,
                          output_tokens=stats.output_tokens, turns=session.turns)
    outcome = replace(outcome, phases=phases, duration_s=round(time.monotonic() - began, 3), error=outcome.error and redact(outcome.error),
                      limits=_limits_record(limits, sources, agent.unenforced_limits()))
    for path in attempt_dir.rglob("*"):
        if path.is_file():
            redact_file(path)
    _write(attempt_dir / "outcome.json", asdict(outcome))
    _write(attempt_dir / "transcript.json", [asdict(m) for m in session.transcript] if session else [])
    return outcome


def _agent_turns(benchmark: Benchmark, task: Task, env: Environment, session: RecordingSession,
                 phases: dict[str, float]) -> Outcome | None:
    lost = None
    try:
        try:
            benchmark.run_agent(task, session, env)
        finally:
            phases["agent_end"] = time.time()
    except TurnLimit:
        pass
    except AgentLimitation as e:
        session.limitation = f"AgentLimitation: {e}"
    except EnvironmentLost as e:
        lost = e
    if died := _container_death(env):
        return died
    if lost:
        raise lost
    return None


def _container_death(env: Environment) -> Outcome | None:
    state = env.state()
    if state is None:
        return Outcome("infra_error", error="container lost: gone, or Docker unreachable", retryable=True)
    if state.running:
        return None
    if state.end_reason is EndReason.KILLED:
        why = "container died during agent turn"
    elif state.end_reason is EndReason.RESOURCE_LIMIT:
        why = "container exceeded its resource limits during agent turn"
    elif state.end_reason is EndReason.EXPIRED:
        return Outcome("infra_error", error="container lifetime expired", retryable=True)
    else:
        return Outcome("infra_error", error="container ended for an unknown reason", retryable=True)
    score = Score(False, why, {"end_reason": state.end_reason.value})
    return Outcome("graded", score, 0.0)


def _limits_record(limits: Limits, sources: dict[str, str], unenforced: frozenset[str]) -> dict[str, dict]:
    return {name: {"value": value, "source": sources.get(name, "default")} | ({"enforced": False} if name in unenforced else {})
            for name, value in asdict(limits).items()}


def _grade(benchmark: Benchmark, task: Task, env: Environment, session: RecordingSession,
           phases: dict[str, float], grade_timeout_s: float) -> Outcome:
    phases["grade_start"] = time.time()
    try:
        return _graded(benchmark, task, env, session, grade_timeout_s)
    finally:
        phases["grade_end"] = time.time()


def _graded(benchmark: Benchmark, task: Task, env: Environment, session: RecordingSession, grade_timeout_s: float) -> Outcome:
    try:
        session.close()
        if benchmark.kill_before_grading:
            env.kill_agent_processes()  # nothing the agent started may touch the world while it is graded
        _save_workspace(env, session)  # the agent's end state, before the grader adds anything
        with env.attach_grader(benchmark.grader_image(task)) as grader:
            deadline = time.monotonic() + grade_timeout_s
            score = benchmark.grade(task, GradingDeadline(grader, deadline, grade_timeout_s),
                                    GradingDeadline(env, deadline, grade_timeout_s), tuple(session.transcript))
    except GraderError as e:
        return Outcome("grader_error", error=str(e))
    except EnvironmentLost:
        # the agent may have killed PID 1 as its last act
        if died := _container_death(env):
            return died
        raise
    try:
        json.dumps(score.value)
        return Outcome("graded", score, benchmark.score_to_float(score))
    except (TypeError, ValueError) as e:
        return Outcome("grader_error", error=f"unusable score value {score.value!r}: {e}")


class GradingDeadline(Environment):
    def __init__(self, env: Environment, deadline: float, grade_timeout_s: float) -> None:
        super().__init__(env.image, env.workdir)
        self.env, self.deadline, self.grade_timeout_s = env, deadline, grade_timeout_s

    def exec(self, cmd: str | list[str], timeout: float | None = None, cwd: str | None = None,
             env: dict[str, str] | None = None, user: str | None = None) -> ExecResult:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise GraderError(f"grading ran past its grade timeout ({self.grade_timeout_s:g}s)")
        return self.env.exec(cmd, timeout=left if timeout is None else min(timeout, left), cwd=cwd, env=env, user=user)

    def start(self) -> None:
        self.env.start()

    def stop(self) -> None:
        self.env.stop()

    def state(self) -> EnvironmentState | None:
        return self.env.state()

    def kill_agent_processes(self) -> None:
        self.env.kill_agent_processes()

    def save_workspace(self, local: Path, max_bytes: int) -> bool:
        return self.env.save_workspace(local, max_bytes)

    def attach(self, sidecar: Sidecar) -> Environment:
        return self.env.attach(sidecar)

    def write_file(self, path: str, content: str | bytes) -> None:
        self.env.write_file(path, content)

    def read_file(self, path: str) -> bytes:
        return self.env.read_file(path)

    def upload(self, local: Path, remote: str) -> None:
        self.env.upload(local, remote)

    def download(self, remote: str, local: Path) -> None:
        self.env.download(remote, local)


def _save_workspace(env: Environment, session: RecordingSession) -> None:
    path = session.log.path.parent / "workspace.tar"
    try:
        truncated = env.save_workspace(path, WORKSPACE_MAX_BYTES)
    except OSError as e:
        session.log.write("workspace_saved", {"error": str(e)})
        return
    session.log.write("workspace_saved", {"file": path.name, "bytes": path.stat().st_size, "truncated": truncated})


def _write(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(redact(json.dumps(data, indent=2, default=str)))  # default=str: a stray object never blocks the write
    os.replace(tmp, path)
