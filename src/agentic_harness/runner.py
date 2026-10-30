"""The trial runner: one attempt at one task, start to finish (`ideation/agents-and-runner.md`)."""

import json
import time
import traceback
from dataclasses import asdict, replace
from pathlib import Path

from agentic_harness.agents.base import Agent, AgentLimitation, AgentSession, Unsupported
from agentic_harness.benchmarks.base import Benchmark, GraderError
from agentic_harness.environments.docker import DockerEnvironment, DockerError
from agentic_harness.types import Limits, Message, Outcome, Score, Task

GRADE_MARGIN_S = 600  # the container outlives the trial deadline by this much, so grading has time to finish


class TurnLimit(Exception):
    """The benchmark tried to send more than limits.max_turns messages."""


class RecordingSession(AgentSession):
    """What the benchmark is handed: forwards to the agent's session, records both sides, and caps the turns."""

    def __init__(self, inner: AgentSession, max_turns: int) -> None:
        self.inner = inner
        self.max_turns = max_turns
        self.turns = 0
        self.transcript: list[Message] = []
        self.events: list[dict] = []  # agent-neutral trace; the events table in Phase 4

    def send(self, message: Message) -> Message:
        if self.turns >= self.max_turns:
            raise TurnLimit(f"more than {self.max_turns} turns")
        self.turns += 1
        self._record("message_sent", message)
        reply = self.inner.send(message)
        self._record("message_received", reply)
        return reply

    def _record(self, kind: str, message: Message) -> None:
        self.transcript.append(message)
        self.events.append({"seq": len(self.events), "turn": self.turns, "kind": kind, "payload": asdict(message), "ts": time.time()})


def run_trial(benchmark: Benchmark, agent: Agent, task: Task, limits: Limits, attempt_dir: Path) -> Outcome:
    """Run one attempt and write outcome.json, transcript.json and events.json into attempt_dir. Never raises
    (except KeyboardInterrupt): every failure becomes an Outcome."""
    attempt_dir.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    session: RecordingSession | None = None
    try:
        with DockerEnvironment(task.image, lifetime_s=int(limits.trial_timeout_s + GRADE_MARGIN_S)) as env:
            session = RecordingSession(agent.start(env, attempt_dir, limits), limits.max_turns)
            outcome = _agent_turns(benchmark, task, env, session)
            if outcome is None:
                outcome = _grade(benchmark, task, env, session)
    except DockerError as e:
        outcome = Outcome("infra_error", error=f"DockerError: {e}")
    except Unsupported as e:
        outcome = Outcome("unsupported", error=str(e))
    except Exception:
        outcome = Outcome("infra_error", error=traceback.format_exc())  # a harness or agent-adapter crash
    if session is not None:
        outcome = replace(
            outcome, agent_exit_status=session.inner.exit_status, cost_usd=session.inner.cost_usd, turns=session.turns
        )
    outcome = replace(outcome, duration_s=round(time.monotonic() - began, 3))
    _write(attempt_dir / "outcome.json", asdict(outcome))
    _write(attempt_dir / "transcript.json", [asdict(m) for m in session.transcript] if session else [])
    _write(attempt_dir / "events.json", session.events if session else [])
    return outcome


def _agent_turns(benchmark: Benchmark, task: Task, env: DockerEnvironment, session: RecordingSession) -> Outcome | None:
    """Let the benchmark drive the agent. Returns an Outcome only if the container died meanwhile (rule 6)."""
    docker_error = None
    try:
        benchmark.run_agent(task, session, env)
    except TurnLimit:
        pass  # the agent's turns are over; grade what it did
    except AgentLimitation as e:  # the agent can't go on (context full, model refused): grade what it did, like a timeout
        session.inner.exit_status = session.inner.exit_status or f"AgentLimitation: {e}"
    except DockerError as e:
        docker_error = e  # maybe the agent killed its own container: the evidence below decides
    if died := _container_death(benchmark, env):
        return died
    if docker_error:
        raise docker_error
    return None


def _container_death(benchmark: Benchmark, env: DockerEnvironment) -> Outcome | None:
    """Rule 6, from `docker inspect`: None if the container still runs, else how the trial ends."""
    state = env.state()
    if state is None or state["OOMKilled"]:  # gone, Docker unreachable, or out of memory (no limit of ours yet)
        return Outcome("infra_error", error=f"container lost: {state}")
    if not state["Running"]:  # something inside killed the main process: the agent's doing
        score = Score(False, "container died during agent turn", {"state": state})
        return Outcome("graded", score, benchmark.score_to_float(score))
    return None


def _grade(benchmark: Benchmark, task: Task, env: DockerEnvironment, session: RecordingSession) -> Outcome:
    try:
        env.kill_agent_processes()  # nothing the agent started may touch the world while it is graded
        score = benchmark.grade(task, env, tuple(session.transcript))
    except GraderError as e:
        return Outcome("grader_error", error=str(e))
    except DockerError:
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
    path.write_text(json.dumps(data, indent=2, default=str))  # default=str: a stray object never blocks the write
