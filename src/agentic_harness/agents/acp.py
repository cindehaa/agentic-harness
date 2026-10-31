"""Any agent that speaks the Agent Client Protocol (ACP), run inside the trial's world. Agents differ only in data
(an AcpDefinition: how to install it, how to launch it, which variables it needs), so a new or private agent needs
no code here. The harness launches the agent with `docker exec -i` and speaks ACP over its stdin/stdout (the official
`agent-client-protocol` library): one ACP session per trial, one session/prompt per turn. The agent acts with its
own tools (we offer no fs/terminal capabilities); its session/update notifications are our trace."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import tomllib
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass, field, fields
from pathlib import Path

import acp
from acp.connection import StreamEvent
from acp.schema import (
    AllowedOutcome, DeniedOutcome, HttpMcpServer, Implementation, PromptResponse, RequestPermissionResponse, Usage,
)

from agentic_harness.agents.base import (
    Agent, AgentCapabilities, AgentLimitation, AgentSession, AgentStats, PreflightError, TransientError, Unsupported,
)
from agentic_harness.environments import Environment, EnvironmentLost, build_image, image_id
from agentic_harness.recording import OUTPUT_CAP
from agentic_harness.types import Limits, McpServer, Message, Text

BUILT_IN = Path(__file__).with_name("acp_agents.toml")
STARTUP_TIMEOUT_S = 120.0  # initialize + session/new: the agent's own start-up (Claude Code's takes seconds)
CANCEL_GRACE_S = 15.0  # after session/cancel, how long the agent gets to end its turn before it is killed
STDERR_CAP = 1_000_000  # bytes of the agent's own log kept in agent-stderr.log
# ACP gives provider failures no error kinds, only a message: these words in it mean "try again later" / "the model
# can't go on" (Claude Code's own error names among them)
TRANSIENT_HINTS = ("overloaded", "rate limit", "rate_limit", "server_error", "529", "503", "502")
LIMITATION_HINTS = ("prompt is too long", "prompt_too_long", "context window", "context_length")
# The agent kept a step limit we gave it (limits_meta) by failing the prompt: the trial's limit, not an error
STEP_LIMIT_HINTS = ("maximum number of turns",)  # Claude Code: "Reached maximum number of turns (N)"

_build_lock = threading.Lock()  # worker threads share one Docker; build each derived image once


class StepLimitReached(Exception):
    """The agent stopped at the step limit it was given, reporting it as an error."""


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
    # Limits the agent keeps itself, given to it in session/new's _meta: Limits field -> dotted path in _meta, e.g.
    # {"max_steps": "claudeCode.options.maxTurns"}. ACP has no standard field for them.
    limits_meta: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "AcpDefinition":
        caps = data.get("capabilities", {})
        if unknown := set(data.get("limits_meta", {})) - {f.name for f in fields(Limits)}:
            raise ValueError(f"{name}: limits_meta names no limit {sorted(unknown)}")
        return cls(
            name=name, version=str(data["version"]), command=tuple(data["command"]), install=data["install"],
            install_dir=data["install_dir"], world_setup=data.get("world_setup", ""),
            forward_env=tuple(data.get("forward_env", ())), env=data.get("env", {}), model_env=data.get("model_env"),
            model_prefix=data.get("model_prefix", ""), limits_meta=data.get("limits_meta", {}),
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

    def unenforced_limits(self) -> frozenset[str]:
        """The harness keeps the deadline, the budget, the turns and the container limits itself; an agent's own
        model calls and commands it can't see, so those hold only if the definition passes them on (limits_meta)."""
        return frozenset({"max_steps", "command_timeout_s"} - set(self.definition.limits_meta))

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
        session.open(env.workdir, mcp_servers, _meta(d.limits_meta, limits))
        return session


class AcpSession(AgentSession):
    """Also the ACP client the library calls back (session_update, request_permission). The library is asyncio: it
    runs on this session's own event loop thread, alive as long as the agent's process; send() and the other public
    methods run on the caller's thread and wait for the loop's answers."""

    def __init__(self, env: Environment, proc: subprocess.Popen, model: str, artifacts_dir: Path, limits: Limits) -> None:
        self.env = env
        self.proc = proc
        self.model = model  # litellm-style: prices a turn the agent reports tokens but no cost for
        self.limits = limits
        self.deadline = time.monotonic() + limits.trial_timeout_s
        self.raw_log = (artifacts_dir / "acp.jsonl").open("a")  # every ACP message, both ways
        self.stderr_tail = b""  # the end of the agent's own log, for error messages
        self.lock = threading.Lock()  # the loop thread adds to the trace while send() / new_events() run
        self.session_id: str | None = None
        self.events: list[tuple[str, dict]] = []
        self.calls: dict[str, dict] = {}  # tool calls of this turn not finished yet, by id
        self.text: list[str] = []  # the agent's message since its last tool call: the reply, if the turn ends here
        self.thought: list[str] = []
        self.ended_by: str | None = None  # set when the harness ends a turn: TimeLimit / LimitsExceeded
        self.dead = False  # killed at the deadline: no more turns
        self.ended = False  # the agent's process has ended: nothing more can be asked
        self.exit_status: str | None = None
        self.cost = 0.0
        self.cost_reported = False
        self.input_tokens = 0
        self.output_tokens = 0
        self.handler_error: Exception | None = None  # the library only logs our handlers' exceptions
        ready: Future = Future()
        serve = self._serve(ready, artifacts_dir / "agent-stderr.log")
        threading.Thread(target=asyncio.run, args=(serve,), daemon=True).start()
        ready.result(STARTUP_TIMEOUT_S)

    async def _serve(self, ready: Future, stderr_path: Path) -> None:
        """The loop thread's whole life: the library's connection over the process's pipes, then the agent's own log
        until the process ends."""
        self.loop = asyncio.get_running_loop()
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        await self.loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdout), self.proc.stdout)
        await self.loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stderr), self.proc.stderr)
        transport, protocol = await self.loop.connect_write_pipe(asyncio.streams.FlowControlMixin, self.proc.stdin)
        stdin = asyncio.StreamWriter(transport, protocol, None, self.loop)
        self.conn = acp.connect_to_agent(self, stdin, stdout, observers=[self._log])
        ready.set_result(None)
        await self._drain_stderr(stderr, stderr_path)
        self.ended = True
        while not stdout.at_eof():  # what the agent wrote before it ended (e.g. an error answer) is handled first
            await asyncio.sleep(0.05)
        await self.conn.close()  # requests still waiting fail with ConnectionError; asyncio.run then ends the loop
        self.raw_log.close()

    def open(self, cwd: str, mcp_servers: tuple[McpServer, ...], meta: dict | None = None) -> None:
        """initialize + session/new. The definition said what the agent can do (the runner checked that); the agent's
        own answer must agree, or the definition is wrong."""
        # No client capabilities (no fs / terminal): the agent works in the world with its own tools
        init = self._call("initialize", self.conn.initialize, protocol_version=acp.PROTOCOL_VERSION,
                          client_info=Implementation(name="agentic-harness", version="0"))
        if init.protocol_version != acp.PROTOCOL_VERSION:
            raise AcpAgentError(f"agent speaks ACP version {init.protocol_version}, we speak {acp.PROTOCOL_VERSION}")
        if mcp_servers and not init.agent_capabilities.mcp_capabilities.http:
            raise AcpAgentError("the definition says mcp, but the agent doesn't take MCP servers over HTTP")
        servers = [HttpMcpServer(type="http", name=s.name, url=s.url, headers=[]) for s in mcp_servers]
        new = self._call("session/new", self.conn.new_session, cwd=cwd, mcp_servers=servers, **(meta or {}))
        self.session_id = new.session_id
        # the model the agent says it runs: a session config option of category "model" (ACP's generic form)
        model = next((o.current_value for o in new.config_options or [] if o.category == "model"), None)
        agent = init.agent_info.model_dump(by_alias=True, exclude_none=True) if init.agent_info else None
        self._trace("session", {"agent": agent, "session_id": self.session_id, "model": model})

    def send(self, message: Message) -> Message:
        if not all(isinstance(c, Text) for c in message.content):
            raise Unsupported("ACP agents are only sent Text")
        remaining = self.deadline - time.monotonic()
        if self.dead or remaining <= 0 or self.cost >= self.limits.cost_usd:  # nothing left for this turn
            self.exit_status = "TimeLimit" if self.dead or remaining <= 0 else "LimitsExceeded"
            return Message("agent", (Text(""),))
        self.text, self.thought, self.ended_by = [], [], None
        prompt = [acp.text_block(c.text) for c in message.content]
        future = self._submit("session/prompt", self.conn.prompt, session_id=self.session_id, prompt=prompt)
        try:
            result = self._wait(future, "session/prompt", remaining)
        except TimeoutError:
            self.ended_by = "TimeLimit"
            result = self._cancel(future)
        except StepLimitReached:
            self.ended_by, result = "LimitsExceeded", None
        finally:
            self._end_turn()
            if self.handler_error:
                raise TransientError(f"our ACP handler failed: {self.handler_error!r}") from self.handler_error
        self._count(result.usage if result else None)
        stop = result.stop_reason if result else None
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

    def _submit(self, method: str, call, **params) -> Future:
        """Runs one of the library's calls on the loop thread; its answer (or exception) comes back in the Future."""
        if self.ended:
            self._lost(f"{method}: the agent's process has ended")
        return asyncio.run_coroutine_threadsafe(call(**params), self.loop)

    def _call(self, method: str, call, **params):
        timeout = min(STARTUP_TIMEOUT_S, self.deadline - time.monotonic())
        return self._wait(self._submit(method, call, **params), method, timeout)

    def _wait(self, future: Future, method: str, timeout: float):
        try:
            return future.result(max(timeout, 0))  # TimeoutError: the request stays open (the caller may cancel)
        except (ConnectionError, CancelledError):
            self._lost(f"{method}: the agent's process ended before answering")
        except acp.RequestError as e:
            error = f"{method}: {e} (code {e.code})" + (f": {json.dumps(e.data)}" if e.data is not None else "")
            text = error.lower()
            if any(h in text for h in STEP_LIMIT_HINTS):
                raise StepLimitReached(error) from None
            if any(h in text for h in LIMITATION_HINTS):
                raise AgentLimitation(error) from None
            if any(h in text for h in TRANSIENT_HINTS):
                raise TransientError(error) from None
            raise AcpAgentError(f"{error}{self._stderr_tail()}") from None

    def _lost(self, what: str):
        """The agent's process ended. If the world went with it, the runner decides whose fault that was."""
        state = self.env.state()
        if state is None or not state.running:
            raise EnvironmentLost(f"{what}: the world is gone")
        raise AcpAgentError(f"{what}{self._stderr_tail()}")

    def _cancel(self, prompt: Future) -> PromptResponse | None:
        """End the turn: session/cancel, and if the agent doesn't end it in CANCEL_GRACE_S, kill it (killing the
        docker exec client leaves the agent running inside, hence kill_agent_processes)."""
        try:
            asyncio.run_coroutine_threadsafe(self.conn.cancel(session_id=self.session_id), self.loop)
            return prompt.result(CANCEL_GRACE_S)
        except (TimeoutError, ConnectionError, CancelledError, acp.RequestError):
            self.dead = True
            self.proc.kill()
            self.env.kill_agent_processes()
            return None

    # --- what the agent sends us (the library calls these on the loop thread)

    async def request_permission(self, session_id: str, tool_call, options, **kwargs) -> RequestPermissionResponse:
        try:
            # The world is the sandbox: everything inside it is allowed. Once, never "always": an always-rule may be
            # written into the workspace (e.g. Claude Code's .claude/settings.local.json) and graded with it.
            chosen = next((o for kind in ("allow_once", "allow_always") for o in options if o.kind == kind), None)
            self._trace("permission", {"tool_call": tool_call.tool_call_id, "granted": chosen.kind if chosen else None})
            if chosen is None:
                return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
            return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=chosen.option_id))
        except Exception as e:
            self.handler_error = self.handler_error or e
            raise

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        try:
            if session_id != self.session_id:
                return
            u = update.model_dump(mode="json", by_alias=True, exclude_unset=True)  # the fields the agent sent
            kind = u.get("sessionUpdate")
            cancel = False
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
                        self.ended_by, cancel = "LimitsExceeded", True
            if cancel:  # outside the lock: send() may be waiting for it
                await self.conn.cancel(session_id=self.session_id)
        except Exception as e:
            self.handler_error = self.handler_error or e
            raise

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

    def _count(self, usage: Usage | None) -> None:
        """One turn's tokens (cached ones counted as input, as litellm's prompt_tokens do), and its cost if the agent
        reported none: priced with litellm, 0 if litellm doesn't know the model."""
        if usage is None:
            return
        cached_read, cached_write = usage.cached_read_tokens or 0, usage.cached_write_tokens or 0
        tokens_in = usage.input_tokens + cached_read + cached_write
        tokens_out = usage.output_tokens
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

    def _log(self, event: StreamEvent) -> None:
        entry = {"dir": event.direction.value, "ts": time.time(), "message": event.message}  # dir: outgoing / incoming
        self.raw_log.write(json.dumps(entry) + "\n")
        self.raw_log.flush()

    async def _drain_stderr(self, stderr: asyncio.StreamReader, path: Path) -> None:
        """The agent's own log, read as it comes (a full pipe would block the agent) until the process ends: kept to
        STDERR_CAP bytes."""
        kept = 0
        with path.open("wb") as f:
            while chunk := await stderr.read(65536):
                f.write(chunk[: STDERR_CAP - kept])
                f.flush()
                kept = min(STDERR_CAP, kept + len(chunk))
                self.stderr_tail = (self.stderr_tail + chunk)[-2000:]

    def _stderr_tail(self) -> str:
        tail = self.stderr_tail.decode("utf-8", errors="replace").strip()
        return f"; agent stderr: {tail}" if tail else ""


def _meta(limits_meta: dict[str, str], limits: Limits) -> dict:
    """session/new's _meta: each limit the agent keeps itself, at its dotted path."""
    meta: dict = {}
    for name, path in limits_meta.items():
        *parents, key = path.split(".")
        node = meta
        for part in parents:
            node = node.setdefault(part, {})
        node[key] = getattr(limits, name)
    return meta


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
