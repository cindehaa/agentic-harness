"""Any agent that speaks the Agent Client Protocol (ACP), run inside the trial's world. Agents differ only in data
(an AcpDefinition: how to install it, how to launch it, which variables it needs), so a new or private agent needs
no code here. The harness launches the agent with `docker exec -i` and speaks ACP over its stdin/stdout: one ACP
session per trial, one session/prompt per turn. The agent acts with its own tools (we offer no fs/terminal
capabilities); its session/update notifications are our trace."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agentic_harness.agents.acp_client import AcpClosed, AcpConnection, AcpError
from agentic_harness.agents.base import (
    Agent, AgentCapabilities, AgentLimitation, AgentSession, AgentStats, PreflightError, TransientError, Unsupported,
)
from agentic_harness.environments import Environment, EnvironmentLost, build_image, image_id
from agentic_harness.recording import OUTPUT_CAP
from agentic_harness.types import Limits, McpServer, Message, Text

PROTOCOL_VERSION = 1
BUILT_IN = Path(__file__).with_name("acp_agents.toml")
STARTUP_TIMEOUT_S = 120.0  # initialize + session/new: the agent's own start-up (Claude Code's takes seconds)
CANCEL_GRACE_S = 15.0  # after session/cancel, how long the agent gets to end its turn before it is killed
STDERR_CAP = 1_000_000  # bytes of the agent's own log kept in agent-stderr.log
# ACP gives provider failures no error kinds, only a message: these words in it mean "try again later" / "the model
# can't go on" (Claude Code's own error names among them)
TRANSIENT_HINTS = ("overloaded", "rate limit", "rate_limit", "server_error", "529", "503", "502")
LIMITATION_HINTS = ("prompt is too long", "prompt_too_long", "context window", "context_length")

_build_lock = threading.Lock()  # worker threads share one Docker; build each derived image once


class AcpAgentError(RuntimeError):
    """The agent failed in a way another attempt would repeat (bad key, unknown model, crashed, a definition that
    doesn't match the agent): infra_error, not retried."""


@dataclass(frozen=True)
class AcpDefinition:
    """How to put an ACP agent in a world and talk to it. Built-in ones live in acp_agents.toml; a private agent
    brings its own file with the same keys."""

    name: str  # registry name, or the definition file's path (runs.agent: a worker finds it again from that)
    version: str  # recorded in runs.versions
    command: tuple[str, ...]  # launches the agent in the world; it speaks ACP on stdin/stdout
    # libc ("musl", "glibc", or "any") -> {"from": image, "run": shell}: a build stage that leaves a self-contained
    # install in install_dir; the world gets the one matching its libc
    install: dict[str, dict[str, str]]
    install_dir: str
    world_setup: str = ""  # shell run in the world's image after the copy (e.g. a runtime library alpine lacks)
    forward_env: tuple[str, ...] = ()  # host variables the agent gets (its API key); redacted from every log
    env: dict[str, str] = field(default_factory=dict)  # fixed variables for the agent
    model_env: str | None = None  # the variable the agent reads its model from; None: the agent's own default
    model_prefix: str = ""  # the litellm provider prefix the agent's model names drop, e.g. "anthropic/"
    capabilities: AgentCapabilities = AgentCapabilities(multi_turn=True)  # every ACP session takes many prompts

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "AcpDefinition":
        caps = data.get("capabilities", {})
        return cls(
            name=name, version=str(data["version"]), command=tuple(data["command"]), install=data["install"],
            install_dir=data["install_dir"], world_setup=data.get("world_setup", ""),
            forward_env=tuple(data.get("forward_env", ())), env=data.get("env", {}), model_env=data.get("model_env"),
            model_prefix=data.get("model_prefix", ""),
            capabilities=AgentCapabilities(frozenset(caps.get("content_types", ["text"])), True, caps.get("mcp", False)),
        )


def built_in_definitions() -> dict[str, AcpDefinition]:
    with BUILT_IN.open("rb") as f:
        return {name: AcpDefinition.from_dict(name, data) for name, data in tomllib.load(f).items()}


def load_definition(path: Path) -> AcpDefinition:
    """A private agent's definition file: one agent, the same keys as a table in acp_agents.toml."""
    with path.open("rb") as f:
        return AcpDefinition.from_dict(str(path), tomllib.load(f))


def dockerfile(d: AcpDefinition, base: str) -> str:
    """The world's image: the task's own plus the agent's install. Each install stage is built; the final stage copies
    the one matching the task image's libc, through bind mounts so the others never land in a layer."""
    stages = "".join(f"FROM {s['from']} AS acp-{libc}\n" + (f"RUN {s['run']}\n" if s.get("run") else "") + "\n"
                     for libc, s in sorted(d.install.items()))
    mounts = " ".join(f"--mount=from=acp-{libc},source={d.install_dir},target=/mnt/{libc}" for libc in sorted(d.install))
    pick = ("if [ -e /lib/ld-musl-$(uname -m).so.1 ] && [ -d /mnt/musl ]; then src=/mnt/musl; "
            "elif [ -d /mnt/glibc ]; then src=/mnt/glibc; else src=/mnt/any; fi; "
            f'mkdir -p {d.install_dir} && cp -a "$src/." {d.install_dir}/')
    return f"{stages}FROM {base}\nRUN {mounts} {pick}\n" + (f"RUN {d.world_setup}\n" if d.world_setup else "")


def derived_tag(d: AcpDefinition, base_image_id: str) -> str:
    """Changes with the task image's content and with the definition's install."""
    digest = hashlib.sha256((base_image_id + "\0" + dockerfile(d, "")).encode()).hexdigest()[:12]
    name = re.sub(r"[^a-z0-9._-]+", "-", Path(d.name).stem.lower()).strip("-.") or "agent"
    return f"harness-acp-{name}:{digest}"


class AcpAgent(Agent):
    def __init__(self, definition: AcpDefinition, model: str) -> None:
        super().__init__(model)
        self.definition = definition
        self.name = definition.name
        self.capabilities = definition.capabilities
        self.forward_env = definition.forward_env

    def native_model(self) -> str:
        return self.model.removeprefix(self.definition.model_prefix)

    def preflight(self) -> None:
        """The model is one the agent runs, and its variables are set. No API call: that would spend tokens."""
        prefix = self.definition.model_prefix
        if prefix and "/" in self.model and not self.model.startswith(prefix):
            raise PreflightError(f"{self.name} runs {prefix}* models only, not {self.model!r}")
        missing = [name for name in self.forward_env if not os.environ.get(name)]
        if missing:
            raise PreflightError(f"{self.name} needs {', '.join(missing)} (set it in .env)")

    def version(self) -> str:
        return self.definition.version

    def image_for(self, task_image: str) -> str:
        base = image_id(task_image)
        return task_image if base is None else derived_tag(self.definition, base)

    def prepare_image(self, task_image: str) -> str:
        tag = self.image_for(task_image)
        if tag == task_image:
            return task_image  # not local: starting the world fails as usual (infra_error)
        with _build_lock:
            if image_id(tag) is None:
                with tempfile.TemporaryDirectory() as empty:  # nothing is copied from the host
                    build_image(dockerfile(self.definition, task_image), Path(empty), tag)
        return tag

    def start(self, env: Environment, artifacts_dir: Path, limits: Limits, mcp_servers: tuple[McpServer, ...] = ()) -> "AcpSession":
        d = self.definition
        env_vars = {name: os.environ[name] for name in self.forward_env if name in os.environ} | d.env
        if d.model_env:
            env_vars[d.model_env] = self.native_model()
        proc = env.start_process(list(d.command), env=env_vars)
        session = AcpSession(env, proc, self.model, artifacts_dir, limits)
        session.open(env.workdir, mcp_servers)
        return session


class AcpSession(AgentSession):
    def __init__(self, env: Environment, proc: subprocess.Popen, model: str, artifacts_dir: Path, limits: Limits) -> None:
        self.env = env
        self.proc = proc
        self.model = model  # litellm-style: prices a turn the agent reports tokens but no cost for
        self.limits = limits
        self.deadline = time.monotonic() + limits.trial_timeout_s
        self.raw_log = (artifacts_dir / "acp.jsonl").open("a")  # every ACP message, both ways
        self.stderr_tail = b""  # the end of the agent's own log, for error messages
        self.stderr_thread = threading.Thread(target=self._drain_stderr, args=(artifacts_dir / "agent-stderr.log",), daemon=True)
        self.stderr_thread.start()
        self.lock = threading.Lock()  # the reader thread adds to the trace while send() / new_events() run
        self.session_id: str | None = None
        self.events: list[tuple[str, dict]] = []
        self.calls: dict[str, dict] = {}  # tool calls of this turn not finished yet, by id
        self.text: list[str] = []  # the agent's message since its last tool call: the reply, if the turn ends here
        self.thought: list[str] = []
        self.ended_by: str | None = None  # set when the harness ends a turn: TimeLimit / LimitsExceeded
        self.dead = False  # killed at the deadline: no more turns
        self.exit_status: str | None = None
        self.cost = 0.0
        self.cost_reported = False
        self.input_tokens = 0
        self.output_tokens = 0
        self.conn = AcpConnection(proc.stdin, proc.stdout, self._on_request, self._on_notification, self._log)

    def open(self, cwd: str, mcp_servers: tuple[McpServer, ...]) -> None:
        """initialize + session/new. The definition said what the agent can do (the runner checked that); the agent's
        own answer must agree, or the definition is wrong."""
        init = self._call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            # No fs / terminal: the agent works in the world with its own tools, so we serve no files or commands
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "agentic-harness", "version": "0"},
        })
        if init.get("protocolVersion") != PROTOCOL_VERSION:
            raise AcpAgentError(f"agent speaks ACP version {init.get('protocolVersion')}, we speak {PROTOCOL_VERSION}")
        caps = init.get("agentCapabilities") or {}
        if mcp_servers and not (caps.get("mcpCapabilities") or {}).get("http"):
            raise AcpAgentError("the definition says mcp, but the agent doesn't take MCP servers over HTTP")
        new = self._call("session/new", {
            "cwd": cwd,
            "mcpServers": [{"type": "http", "name": s.name, "url": s.url, "headers": []} for s in mcp_servers],
        })
        self.session_id = new["sessionId"]
        # the model the agent says it runs: a session config option of category "model" (ACP's generic form)
        model = next((o.get("currentValue") for o in new.get("configOptions") or [] if o.get("category") == "model"), None)
        self._trace("session", {"agent": init.get("agentInfo"), "session_id": self.session_id, "model": model})

    def send(self, message: Message) -> Message:
        if not all(isinstance(c, Text) for c in message.content):
            raise Unsupported("ACP agents are only sent Text")
        remaining = self.deadline - time.monotonic()
        if self.dead or remaining <= 0 or self.cost >= self.limits.cost_usd:  # nothing left for this turn
            self.exit_status = "TimeLimit" if self.dead or remaining <= 0 else "LimitsExceeded"
            return Message("agent", (Text(""),))
        self.text, self.thought, self.ended_by = [], [], None
        prompt = [{"type": "text", "text": c.text} for c in message.content]
        id_ = self._start("session/prompt", {"sessionId": self.session_id, "prompt": prompt})
        try:
            result = self._wait(id_, "session/prompt", remaining)
        except TimeoutError:
            self.ended_by = "TimeLimit"
            result = self._cancel(id_)
        finally:
            self._end_turn()
        if self.conn.failure:  # a bug in our handlers, not the agent's doing
            raise self.conn.failure
        self._count(result.get("usage") or {})
        stop = result.get("stopReason")
        self.exit_status = self.ended_by or stop  # end_turn, max_tokens, max_turn_requests, refusal, cancelled
        if stop == "refusal":
            raise AgentLimitation("refusal")
        return Message("agent", (Text("" if self.ended_by else "".join(self.text)),))

    def stats(self) -> AgentStats:
        # ACP reports no count of model calls; tokens come from each turn's usage (an unstable ACP field)
        return AgentStats(self.exit_status, self.cost, 0, self.input_tokens, self.output_tokens)

    def new_events(self) -> list[tuple[str, dict]]:
        with self.lock:
            events, self.events = self.events, []
        return events

    # --- requests and their failures

    def _call(self, method: str, params: dict) -> dict:
        return self._wait(self._start(method, params), method, min(STARTUP_TIMEOUT_S, self.deadline - time.monotonic()))

    def _start(self, method: str, params: dict) -> int:
        try:
            return self.conn.start_request(method, params)
        except AcpClosed as e:
            self._lost(str(e))

    def _wait(self, id_: int, method: str, timeout: float) -> dict:
        try:
            return self.conn.wait(id_, method, max(timeout, 0))
        except AcpClosed as e:
            self._lost(str(e))
        except AcpError as e:
            text = str(e).lower()
            if any(h in text for h in LIMITATION_HINTS):
                raise AgentLimitation(str(e)) from None
            if any(h in text for h in TRANSIENT_HINTS):
                raise TransientError(str(e)) from None
            raise AcpAgentError(f"{e}{self._stderr_tail()}") from None

    def _lost(self, what: str):
        """The agent's process ended. If the world went with it, the runner decides whose fault that was."""
        state = self.env.state()
        if state is None or not state.running:
            raise EnvironmentLost(f"{what}: the world is gone")
        raise AcpAgentError(f"{what}{self._stderr_tail()}")

    def _cancel(self, id_: int) -> dict:
        """End the turn: session/cancel, and if the agent doesn't end it in CANCEL_GRACE_S, kill it (killing the
        docker exec client leaves the agent running inside, hence kill_agent_processes)."""
        try:
            self.conn.notify("session/cancel", {"sessionId": self.session_id})
            return self.conn.wait(id_, "session/prompt", CANCEL_GRACE_S)
        except (TimeoutError, AcpClosed, AcpError):
            self.dead = True
            self.proc.kill()
            self.env.kill_agent_processes()
            return {}

    # --- what the agent sends us (reader thread)

    def _on_request(self, method: str, params: dict) -> dict:
        if method == "session/request_permission":
            # The world is the sandbox: everything inside it is allowed. Once, never "always": an always-rule may be
            # written into the workspace (e.g. Claude Code's .claude/settings.local.json) and graded with it.
            options = params.get("options") or []
            chosen = next((o for kind in ("allow_once", "allow_always") for o in options if o.get("kind") == kind), None)
            self._trace("permission", {"tool_call": (params.get("toolCall") or {}).get("toolCallId"),
                                       "granted": chosen["kind"] if chosen else None})
            if chosen is None:
                return {"outcome": {"outcome": "cancelled"}}
            return {"outcome": {"outcome": "selected", "optionId": chosen["optionId"]}}
        raise LookupError(method)  # fs/*, terminal/*, ...: not offered in initialize

    def _on_notification(self, method: str, params: dict) -> None:
        if method != "session/update" or params.get("sessionId") != self.session_id:
            return
        u = params.get("update") or {}
        kind = u.get("sessionUpdate")
        with self.lock:
            if kind == "agent_message_chunk":
                self.text.append(_text(u.get("content")))
            elif kind == "agent_thought_chunk":
                self.thought.append(_text(u.get("content")))
            elif kind in ("tool_call", "tool_call_update"):
                self._tool(u)
            elif kind == "plan":
                self.events.append(("plan", {"entries": u.get("entries")}))
            elif kind == "usage_update" and isinstance(u.get("cost"), dict):
                self.cost = max(self.cost, float(u["cost"].get("amount") or 0))  # cumulative over the session
                self.cost_reported = True
                if self.cost >= self.limits.cost_usd and self.ended_by is None:
                    self.ended_by = "LimitsExceeded"
                    self.conn.notify("session/cancel", {"sessionId": self.session_id})

    def _tool(self, u: dict) -> None:
        """Gathers a tool call's updates (its input may arrive after the call itself); traced once it ends."""
        id_ = u.get("toolCallId")
        if id_ not in self.calls:
            self._flush_text()
            self.calls[id_] = {"id": id_}
        call = self.calls[id_]
        for key, value in u.items():
            if key not in ("sessionUpdate", "toolCallId", "_meta") and value is not None:
                call[key] = value
        if call.get("status") in ("completed", "failed"):
            self._finish(id_)

    def _finish(self, id_: str) -> None:
        call = self.calls.pop(id_)
        self.events.append(("tool_call", {"id": id_, "name": call.get("name") or call.get("title"), "title": call.get("title"),
                                          "kind": call.get("kind"), "input": call.get("rawInput")}))
        output = _tool_output(call)
        self.events.append(("tool_result", {"id": id_, "status": call.get("status"), "content": output[:OUTPUT_CAP],
                                            "truncated": len(output) > OUTPUT_CAP, "is_error": call.get("status") == "failed"}))

    def _flush_text(self) -> None:
        if self.thought:
            self.events.append(("thought", {"text": "".join(self.thought)}))
            self.thought = []
        if self.text:
            self.events.append(("agent_text", {"text": "".join(self.text)}))
            self.text = []

    def _end_turn(self) -> None:
        with self.lock:
            for id_ in list(self.calls):  # never ended: cut off, or the agent didn't say
                self.calls[id_].setdefault("status", "unfinished")
                self._finish(id_)
            if self.thought:
                self.events.append(("thought", {"text": "".join(self.thought)}))
                self.thought = []

    def _trace(self, kind: str, payload: dict) -> None:
        with self.lock:
            self.events.append((kind, payload))

    def _count(self, usage: dict) -> None:
        """One turn's tokens (cached ones counted as input, as litellm's prompt_tokens do), and its cost if the agent
        reported none: priced with litellm, 0 if litellm doesn't know the model."""
        cached_read, cached_write = usage.get("cachedReadTokens") or 0, usage.get("cachedWriteTokens") or 0
        tokens_in = (usage.get("inputTokens") or 0) + cached_read + cached_write
        tokens_out = usage.get("outputTokens") or 0
        self.input_tokens += tokens_in
        self.output_tokens += tokens_out
        if self.cost_reported or not (tokens_in or tokens_out):
            return
        import litellm  # here, not at the top: only an agent that reports no cost needs it, and importing it is slow

        try:
            prompt, completion = litellm.cost_per_token(model=self.model, prompt_tokens=tokens_in, completion_tokens=tokens_out,
                                                        cache_creation_input_tokens=cached_write, cache_read_input_tokens=cached_read)
        except Exception:
            return
        self.cost += prompt + completion

    def _log(self, direction: str, message: dict) -> None:
        self.raw_log.write(json.dumps({"dir": direction, "ts": time.time(), "message": message}) + "\n")
        self.raw_log.flush()

    def _drain_stderr(self, path: Path) -> None:
        """The agent's own log, read as it comes (a full pipe would block the agent): kept to STDERR_CAP bytes."""
        kept = 0
        with path.open("wb") as f:
            for chunk in iter(lambda: self.proc.stderr.read1(65536), b""):
                f.write(chunk[: STDERR_CAP - kept])
                f.flush()
                kept = min(STDERR_CAP, kept + len(chunk))
                self.stderr_tail = (self.stderr_tail + chunk)[-2000:]

    def _stderr_tail(self) -> str:
        tail = self.stderr_tail.decode("utf-8", errors="replace").strip()
        return f"; agent stderr: {tail}" if tail else ""


def _text(content: dict | None) -> str:
    return (content or {}).get("text", "") if (content or {}).get("type") == "text" else ""


def _tool_output(call: dict) -> str:
    """What a tool call returned, as text: its raw output if the agent sent one, else its displayed content."""
    raw = call.get("rawOutput")
    if raw is not None:
        return raw if isinstance(raw, str) else json.dumps(raw)
    parts = []
    for c in call.get("content") or []:
        if c.get("type") == "content":
            parts.append(_text(c.get("content")) or json.dumps(c.get("content")))
        elif c.get("type") == "diff":
            parts.append(f"diff {c.get('path')}:\n{c.get('newText')}")
        else:
            parts.append(json.dumps(c))
    return "\n".join(parts)
