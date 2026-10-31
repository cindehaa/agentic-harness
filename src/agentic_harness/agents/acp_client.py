"""The client side of the Agent Client Protocol (https://agentclientprotocol.com): JSON-RPC 2.0 over an agent
process's stdin/stdout, one JSON message per line. Synchronous on purpose: a trial's session blocks in send() anyway,
so one reader thread per connection is all the concurrency needed (the official Python SDK is asyncio)."""

import json
import queue
import threading
from collections.abc import Callable
from typing import IO

METHOD_NOT_FOUND = -32601  # JSON-RPC's code for a method we don't offer (e.g. fs/*, terminal/*)
INTERNAL_ERROR = -32603


class AcpError(RuntimeError):
    """The agent answered a request with a JSON-RPC error."""

    def __init__(self, method: str, error: dict) -> None:
        super().__init__(f"{method}: {error.get('message')} (code {error.get('code')}){_data(error)}")
        self.code = error.get("code")


class AcpClosed(RuntimeError):
    """The agent's process closed its stdout (exited or was killed) before answering."""


def _data(error: dict) -> str:
    return f": {json.dumps(error['data'])}" if error.get("data") is not None else ""


class AcpConnection:
    """One agent process. `on_request(method, params) -> result` answers the agent's requests (raise LookupError for a
    method we don't offer); `on_notification(method, params)` gets its notifications (session/update). Both run on the
    reader thread, in the order the agent sent them, so every update of a turn is handled before its response is."""

    def __init__(self, stdin: IO[bytes], stdout: IO[bytes], on_request: Callable[[str, dict], dict],
                 on_notification: Callable[[str, dict], None], on_message: Callable[[str, dict], None] = lambda d, m: None) -> None:
        self.stdin = stdin
        self.on_request = on_request
        self.on_notification = on_notification
        self.on_message = on_message  # every message, both ways ("sent" / "received"): the raw log
        self.next_id = 0
        self.pending: dict[int, queue.Queue] = {}
        self.lock = threading.Lock()  # one writer at a time: a request and a reply to the agent may race
        self.closed = False
        self.failure: Exception | None = None  # the first exception a handler raised; the session re-raises it
        self.reader = threading.Thread(target=self._read, args=(stdout,), daemon=True)
        self.reader.start()

    def request(self, method: str, params: dict, timeout: float | None) -> dict:
        """Send a request and wait for its result."""
        return self.wait(self.start_request(method, params), method, timeout)

    def start_request(self, method: str, params: dict) -> int:
        """Send a request; its id, for wait()."""
        with self.lock:
            self.next_id += 1
            id_ = self.next_id
            self.pending[id_] = queue.Queue(maxsize=1)
        self._send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})
        return id_

    def wait(self, id_: int, method: str, timeout: float | None) -> dict:
        """The request's result. TimeoutError if none comes in `timeout` seconds: the request stays open, so the
        caller can act (e.g. cancel the turn) and wait again."""
        try:
            reply = self.pending[id_].get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"{method}: no answer in {timeout:.0f}s") from None
        self.pending.pop(id_, None)
        if reply is None:
            raise AcpClosed(f"{method}: the agent's process ended before answering")
        if "error" in reply:
            raise AcpError(method, reply["error"])
        return reply.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict) -> None:
        if self.closed:
            raise AcpClosed(f"{message.get('method', 'reply')}: the agent's process has ended")
        line = json.dumps(message) + "\n"  # json.dumps never emits a raw newline, as the transport requires
        with self.lock:
            self.on_message("sent", message)
            try:
                self.stdin.write(line.encode())
                self.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):  # ValueError: already closed on our side
                raise AcpClosed("the agent's process has ended") from None

    def _read(self, stdout: IO[bytes]) -> None:
        for line in stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # the spec forbids non-ACP output on stdout; a misbehaving agent's noise is skipped
            if not isinstance(message, dict):
                continue
            self.on_message("received", message)
            if "method" in message:
                try:
                    self._handle(message)
                except Exception as e:  # a bug in a handler must not stop the reading: the turn would hang
                    self.failure = self.failure or e
            elif (waiting := self.pending.get(message.get("id"))) is not None:
                waiting.put(message)
        self.closed = True
        for waiting in list(self.pending.values()):
            if waiting.empty():
                waiting.put(None)  # wakes every request still waiting: AcpClosed

    def _handle(self, message: dict) -> None:
        method, params = message["method"], message.get("params") or {}
        if "id" not in message:  # a notification: no reply
            self.on_notification(method, params)
            return
        try:
            reply = {"result": self.on_request(method, params)}
        except LookupError:
            reply = {"error": {"code": METHOD_NOT_FOUND, "message": f"method not found: {method}"}}
        except Exception as e:
            self.failure = self.failure or e
            reply = {"error": {"code": INTERNAL_ERROR, "message": f"{type(e).__name__}: {e}"}}
        try:
            self._send({"jsonrpc": "2.0", "id": message["id"], **reply})
        except AcpClosed:
            pass  # the agent is gone; the reader loop ends on its own
