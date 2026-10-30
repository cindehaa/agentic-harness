"""What the harness records while an agent works: the agent-neutral trace in events.jsonl.

The benchmark is handed a RecordingSession (messages), the agent a RecordingEnvironment (commands); both write to
one EventLog, so the trace reads the same whatever the agent is."""

import json
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from agentic_harness.agents.base import AgentSession, AgentStats
from agentic_harness.environments.base import DEFAULT_TIMEOUT, Environment, EnvironmentState, ExecResult
from agentic_harness.redact import redact
from agentic_harness.types import Message

OUTPUT_CAP = 10_000  # characters of each output stream kept in a command event (the agent's own log has the rest)


class TurnLimit(Exception):
    """The benchmark tried to send more than limits.max_turns messages."""


class EventLog:
    """events.jsonl: one redacted JSON line per event, appended (and flushed, by closing) as it happens, so a crash
    loses nothing already recorded."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seq = 0
        self.turn = 0  # set by the session; commands are filed under the turn they ran in

    def write(self, kind: str, payload: dict) -> None:
        event = {"seq": self.seq, "turn": self.turn, "kind": kind, "payload": payload, "ts": time.time()}
        with self.path.open("a") as f:
            f.write(redact(json.dumps(event, default=str)) + "\n")
        self.seq += 1


class RecordingSession(AgentSession):
    """What the benchmark is handed: forwards to the agent's session, records both sides, and caps the turns.
    `on_turn_end` runs after every turn (the worker loads events.jsonl into the events table then)."""

    def __init__(self, inner: AgentSession, max_turns: int, log: EventLog, on_turn_end: Callable[[], None]) -> None:
        self.inner = inner
        self.max_turns = max_turns
        self.log = log
        self.on_turn_end = on_turn_end
        self.turns = 0
        self.transcript: list[Message] = []
        self.limitation: str | None = None  # set by the runner when the agent raised AgentLimitation

    def send(self, message: Message) -> Message:
        if self.turns >= self.max_turns:
            raise TurnLimit(f"more than {self.max_turns} turns")
        self.turns += 1
        self.log.turn = self.turns
        self._record("message_sent", message)
        reply = self.inner.send(message)
        self._record("message_received", reply)
        self.on_turn_end()
        return reply

    def stats(self) -> AgentStats:
        return self.inner.stats()

    def _record(self, kind: str, message: Message) -> None:
        self.transcript.append(message)
        self.log.write(kind, asdict(message))


class RecordingEnvironment(Environment):
    """What the agent is handed: the trial's Environment, with every exec written to the log as a "command" event
    (command, exit code, output capped at OUTPUT_CAP, duration, turn). Everything else is passed straight through."""

    def __init__(self, env: Environment, log: EventLog) -> None:
        super().__init__(env.image)
        self.env = env
        self.log = log

    def exec(self, cmd: str | list[str], timeout: float | None = DEFAULT_TIMEOUT, cwd: str | None = None) -> ExecResult:
        began = time.monotonic()
        event = {"command": cmd, "cwd": cwd}
        try:
            r = self.env.exec(cmd, timeout=timeout, cwd=cwd)
        except Exception as e:  # e.g. EnvironmentLost: still in the trace
            self.log.write("command", event | {"error": f"{type(e).__name__}: {e}", "duration_s": _since(began)})
            raise
        self.log.write("command", event | {
            "exit_code": r.exit_code, "timed_out": r.timed_out,
            "stdout": r.stdout[:OUTPUT_CAP], "stderr": r.stderr[:OUTPUT_CAP],
            "truncated": r.truncated or max(len(r.stdout), len(r.stderr)) > OUTPUT_CAP,
            "duration_s": _since(began),
        })
        return r

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

    def attach_grader(self, image: str) -> Environment:
        return self.env.attach_grader(image)

    def write_file(self, path: str, content: str | bytes) -> None:
        self.env.write_file(path, content)

    def read_file(self, path: str) -> bytes:
        return self.env.read_file(path)

    def upload(self, local: Path, remote: str) -> None:
        self.env.upload(local, remote)

    def download(self, remote: str, local: Path) -> None:
        self.env.download(remote, local)


def _since(began: float) -> float:
    return round(time.monotonic() - began, 3)
