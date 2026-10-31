import json
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from agentic_harness.agents.base import AgentSession, AgentStats
from agentic_harness.environments.base import DEFAULT_TIMEOUT, Environment, EnvironmentState, ExecResult, Sidecar
from agentic_harness.redact import redact
from agentic_harness.types import Message

OUTPUT_CAP = 10_000


class TurnLimit(Exception):
    ...


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.seq = 0
        self.turn = 0

    def write(self, kind: str, payload: dict) -> None:
        event = {"seq": self.seq, "turn": self.turn, "kind": kind, "payload": payload, "ts": time.time()}
        with self.path.open("a") as f:
            f.write(redact(json.dumps(event, default=str)) + "\n")
        self.seq += 1


class RecordingSession(AgentSession):
    def __init__(self, inner: AgentSession, max_turns: int, log: EventLog, on_turn_end: Callable[[AgentStats], None]) -> None:
        self.inner = inner
        self.max_turns = max_turns
        self.log = log
        self.on_turn_end = on_turn_end
        self.turns = 0
        self.transcript: list[Message] = []
        self.limitation: str | None = None

    def send(self, message: Message) -> Message:
        if self.turns >= self.max_turns:
            raise TurnLimit(f"more than {self.max_turns} turns")
        self.turns += 1
        self.log.turn = self.turns
        self._record("message_sent", message)
        try:
            try:
                reply = self.inner.send(message)
            finally:
                for kind, payload in self.inner.new_events():
                    self.log.write(kind, payload)
            self._record("message_received", reply)
        finally:
            self.on_turn_end(self.inner.stats())
        return reply

    def stats(self) -> AgentStats:
        return self.inner.stats()

    def _record(self, kind: str, message: Message) -> None:
        self.transcript.append(message)
        self.log.write(kind, asdict(message))


class RecordingEnvironment(Environment):
    def __init__(self, env: Environment, log: EventLog) -> None:
        super().__init__(env.image, env.workdir)
        self.env = env
        self.log = log

    def exec(self, cmd: str | list[str], timeout: float | None = DEFAULT_TIMEOUT, cwd: str | None = None,
             env: dict[str, str] | None = None, user: str | None = None) -> ExecResult:
        began = time.monotonic()
        # names only: values may be credentials that redaction doesn't know
        event = {"command": cmd, "cwd": cwd, "env": {name: "***" for name in env or {}}, "user": user}
        try:
            r = self.env.exec(cmd, timeout=timeout, cwd=cwd, env=env, user=user)
        except Exception as e:
            self.log.write("command", event | {"error": f"{type(e).__name__}: {e}", "duration_s": _since(began)})
            raise
        self.log.write("command", event | {
            "exit_code": r.exit_code, "timed_out": r.timed_out,
            "stdout": r.stdout[:OUTPUT_CAP], "stderr": r.stderr[:OUTPUT_CAP],
            "truncated": r.truncated or max(len(r.stdout), len(r.stderr)) > OUTPUT_CAP,
            "duration_s": _since(began),
        })
        return r

    def start_process(self, argv: list[str], env: dict[str, str] | None = None, user: str | None = None):
        self.log.write("process", {"command": argv, "env": {name: "***" for name in env or {}}, "user": user})
        return self.env.start_process(argv, env=env, user=user)

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


def _since(began: float) -> float:
    return round(time.monotonic() - began, 3)
