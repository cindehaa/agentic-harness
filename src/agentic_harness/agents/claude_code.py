"""Claude Code, run inside the trial's world: it executes its own tool calls there, so (unlike mini) it can't act
through our exec from the host. The world starts from a derived image (the task's image + the Claude Code binary),
built once per task image and cached. The API key is forwarded per command, never written into an image. Each
turn is one `claude -p` run; later turns resume its session. Tools from the benchmark's MCP servers reach Claude Code
through its own --mcp-config; nothing is added to the prompt."""

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from agentic_harness.agents.base import (
    Agent, AgentCapabilities, AgentLimitation, AgentSession, AgentStats, PreflightError, TransientError, Unsupported,
)
from agentic_harness.environments import Environment, build_image, image_id
from agentic_harness.recording import OUTPUT_CAP
from agentic_harness.types import Limits, McpServer, Message, Text

VERSION = "2.1.308"  # pinned (npm's "stable" tag on 2026-10-29): a run must not change agent mid-way
PACKAGE_BIN = "/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin"
# npm installs Claude Code's native binary for the stage's libc (musl or glibc) and arch. Both stages are built; the
# final stage copies the one matching the task image's libc, through bind mounts so the other never lands in a layer.
# Claude Code's Bash tool needs bash (not busybox sh), which alpine lacks, hence `apk add bash` there.
DOCKERFILE = """\
FROM node:22-alpine AS musl
RUN npm install -g @anthropic-ai/claude-code@{version}

FROM node:22-bookworm-slim AS glibc
RUN npm install -g @anthropic-ai/claude-code@{version}

FROM {base}
RUN --mount=from=musl,source={bin},target=/mnt/musl --mount=from=glibc,source={bin},target=/mnt/glibc \\
    if [ -e /lib/ld-musl-$(uname -m).so.1 ]; then cp /mnt/musl/claude.exe /usr/local/bin/claude; \\
    else cp /mnt/glibc/claude.exe /usr/local/bin/claude; fi
RUN command -v bash > /dev/null || apk add --no-cache bash
"""
SETTINGS = {
    "IS_SANDBOX": "1",  # --dangerously-skip-permissions refuses to run as root without it
    "DISABLE_AUTOUPDATER": "1",  # keep the pinned version
}
MCP_CONFIG = "/tmp/claude-code-mcp.json"  # in the world, outside the workspace (so not in workspace.tar)
# The `error` Claude Code puts on a failed API call's message, by what it means for the trial
LIMITATIONS = {"prompt_too_long", "refusal"}  # the model can't go on: grade the world as it is
TRANSIENT = {"rate_limit", "server_error", "overloaded"}  # after Claude Code's own retries: infra_error, retried
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504, 529}

_build_lock = threading.Lock()  # worker threads share one Docker; build each derived image once


class ClaudeCodeError(RuntimeError):
    """Claude Code failed in a way another attempt would repeat (bad key, unknown model, crashed): infra_error, not
    retried."""


def derived_tag(base_image_id: str) -> str:
    """The derived image's tag: changes with the task image's content, the Claude Code version and the Dockerfile."""
    text = base_image_id + "\0" + DOCKERFILE.format(version=VERSION, base="", bin=PACKAGE_BIN)
    return f"harness-claude-code:{hashlib.sha256(text.encode()).hexdigest()[:12]}"


def mcp_config(servers: tuple[McpServer, ...]) -> str:
    """Claude Code's --mcp-config file: each server by name, over streamable HTTP."""
    return json.dumps({"mcpServers": {s.name: {"type": "http", "url": s.url} for s in servers}}, indent=2)


def parse_stream(text: str) -> list[dict]:
    """stream-json: one JSON event per line. A line cut off (e.g. killed at the deadline) is skipped."""
    events = []
    for line in text.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def trace_events(stream: list[dict]) -> list[tuple[str, dict]]:
    """Claude Code's tool calls and their results as our trace events: they ran inside the world, past our exec."""
    events = []
    for e in stream:
        for block in (e.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if e.get("type") == "assistant" and block.get("type") == "tool_use":
                events.append(("tool_call", {"id": block.get("id"), "name": block.get("name"), "input": block.get("input")}))
            elif e.get("type") == "user" and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):  # e.g. MCP results: a list of content blocks
                    content = "\n".join(c.get("text", json.dumps(c)) for c in content if isinstance(c, dict))
                content = str(content or "")
                events.append(("tool_result", {"id": block.get("tool_use_id"), "content": content[:OUTPUT_CAP],
                                               "truncated": len(content) > OUTPUT_CAP, "is_error": bool(block.get("is_error"))}))
    return events


def usage(stream: list[dict]) -> tuple[int, int, int]:
    """(input tokens, output tokens, model calls) of one `claude -p` run. From its result event when it has one; a run
    cut off before it (the deadline) counts its assistant messages instead, each by its last event (Claude Code
    prints one per content block, the usage growing). Input counts cached tokens too, as litellm's prompt_tokens does."""
    result = next((e for e in reversed(stream) if e.get("type") == "result"), None)
    if result is not None:
        usages, calls = [result.get("usage") or {}], result.get("num_turns") or 0
    else:
        usages = _message_usages(stream)
        calls = len(usages)
    return sum(_input_tokens(u) for u in usages), sum(u.get("output_tokens") or 0 for u in usages), calls


def _input_tokens(usage: dict) -> int:
    """Anthropic counts cached prompt tokens apart from input_tokens."""
    return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def estimate_cost(model: str, stream: list[dict]) -> float:
    """What a run cut off before its result event spent (Claude Code reports cost only in that event): its tokens
    at litellm's prices. 0 if litellm doesn't know the model."""
    import litellm  # here, not at the top: only this rare path needs it, and importing it is slow

    total = 0.0
    for u in _message_usages(stream):
        try:
            prompt, completion = litellm.cost_per_token(  # its prompt_tokens include the cached ones
                model=f"anthropic/{model}", prompt_tokens=_input_tokens(u), completion_tokens=u.get("output_tokens") or 0,
                cache_creation_input_tokens=u.get("cache_creation_input_tokens") or 0,
                cache_read_input_tokens=u.get("cache_read_input_tokens") or 0)
        except Exception:
            return 0.0
        total += prompt + completion
    return total


def _message_usages(stream: list[dict]) -> list[dict]:
    """The usage of each model call in the stream: its assistant message's last event (one per content block)."""
    by_id = {}
    for e in stream:
        message = e.get("message") or {}
        if e.get("type") == "assistant" and message.get("usage"):
            by_id[message.get("id")] = message["usage"]
    return list(by_id.values())


def check_result(stream: list[dict]) -> dict:
    """The run's result event. Raises AgentLimitation / TransientError / ClaudeCodeError for a failed API call, as
    mini's adapter does for the same litellm errors."""
    result = next((e for e in reversed(stream) if e.get("type") == "result"), None)
    if result is None:
        raise ClaudeCodeError("no result event in Claude Code's output")
    if result.get("is_error") and result.get("terminal_reason") == "api_error":
        failed = next((e for e in reversed(stream) if e.get("type") == "assistant" and e.get("error")), {})
        kind, status = failed.get("error"), result.get("api_error_status")
        what = f"{kind} (HTTP {status}): {result.get('result')}"
        if kind in LIMITATIONS:
            raise AgentLimitation(what)
        if kind in TRANSIENT or status in TRANSIENT_STATUS:
            raise TransientError(what)
        raise ClaudeCodeError(what)  # e.g. authentication_failed, billing_error, model_not_found, invalid_request
    if result.get("stop_reason") == "refusal":
        raise AgentLimitation(f"refusal: {result.get('result')}")
    return result


class ClaudeCodeSession(AgentSession):
    def __init__(self, env: Environment, model: str, artifacts_dir: Path, limits: Limits, env_vars: dict[str, str],
                 use_mcp: bool) -> None:
        self.env = env
        self.model = model
        self.trajectory = artifacts_dir / "trajectory.jsonl"  # Claude Code's raw stream, every turn
        self.limits = limits
        self.deadline = time.monotonic() + limits.trial_timeout_s
        self.env_vars = env_vars
        self.use_mcp = use_mcp
        self.session_id: str | None = None  # Claude Code's own; later turns resume it
        self.turns = 0
        self.exit_status: str | None = None
        self.cost = 0.0
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.pending: list[tuple[str, dict]] = []

    def send(self, message: Message) -> Message:
        if not all(isinstance(c, Text) for c in message.content):
            raise Unsupported("Claude Code adapter only sends Text")
        remaining = self.deadline - time.monotonic()
        steps_left, budget_left = self.limits.max_steps - self.model_calls, self.limits.cost_usd - self.cost
        if remaining <= 0 or steps_left <= 0 or budget_left <= 0:  # nothing left for this turn
            self.exit_status = "TimeLimit" if remaining <= 0 else "LimitsExceeded"
            return Message("agent", (Text(""),))
        self.turns += 1
        out = f"/tmp/claude-code-turn-{self.turns}.jsonl"
        argv = [
            "claude", "-p", "--output-format", "stream-json", "--verbose", "--model", self.model,
            "--dangerously-skip-permissions", "--max-turns", str(steps_left), "--max-budget-usd", f"{budget_left:.4f}",
            "--strict-mcp-config", *(["--mcp-config", MCP_CONFIG] if self.use_mcp else []),
            *(["--resume", self.session_id] if self.session_id else []),
            "--", "\n".join(c.text for c in message.content),
        ]
        # The stream goes to a file in the world, not through exec's capped output, so a long run keeps its result
        # event and a run cut off at the deadline keeps what it did.
        r = self.env.exec(["sh", "-c", 'out=$1; shift; exec "$@" > "$out"', "sh", out, *argv],
                          timeout=remaining, env=self.env_vars)
        if r.timed_out:
            self.env.kill_agent_processes()  # killing the docker exec client leaves claude running inside
        try:
            text = self.env.read_file(out).decode("utf-8", errors="replace")
        except FileNotFoundError:
            raise ClaudeCodeError(f"claude never ran (exit {r.exit_code}): {r.stderr.strip()[-2000:]}") from None
        with self.trajectory.open("a") as f:
            f.write(text)
        stream = parse_stream(text)
        self.pending += trace_events(stream)
        self._count(stream)  # before anything can raise: what this run spent is spent whatever happens next
        if r.timed_out:
            self.exit_status = "TimeLimit"
            return Message("agent", (Text(""),))
        try:
            result = check_result(stream)
        except ClaudeCodeError as e:
            raise ClaudeCodeError(f"{e} (exit {r.exit_code}; stderr: {r.stderr.strip()[-2000:]})") from None
        self.session_id = result.get("session_id")
        self.exit_status = result.get("subtype")  # e.g. success, error_max_turns, error_max_budget_usd
        return Message("agent", (Text(result.get("result") or ""),))

    def _count(self, stream: list[dict]) -> None:
        tokens_in, tokens_out, calls = usage(stream)
        self.input_tokens += tokens_in
        self.output_tokens += tokens_out
        self.model_calls += calls
        result = next((e for e in reversed(stream) if e.get("type") == "result"), None)
        if result is not None and result.get("total_cost_usd") is not None:
            self.cost = result["total_cost_usd"]  # cumulative over the session, resumed turns included
        else:
            self.cost += estimate_cost(self.model, stream)

    def stats(self) -> AgentStats:
        return AgentStats(self.exit_status, self.cost, self.model_calls, self.input_tokens, self.output_tokens)

    def new_events(self) -> list[tuple[str, dict]]:
        events, self.pending = self.pending, []
        return events


class ClaudeCode(Agent):
    name = "claude-code"
    model_env = "MSWEA_MODEL_NAME"  # the same model setting as mini, so both agents run the same model by default
    capabilities = AgentCapabilities(content_types=frozenset({"text"}), multi_turn=True, mcp=True)
    forward_env = ("ANTHROPIC_API_KEY",)  # the only host variables that enter the world, per command

    def preflight(self) -> None:
        """An Anthropic model and its API key. No API call: that would spend tokens."""
        if "/" in self.model and not self.model.startswith("anthropic/"):
            raise PreflightError(f"Claude Code runs Anthropic models only, not {self.model!r}")
        missing = [name for name in self.forward_env if not os.environ.get(name)]
        if missing:
            raise PreflightError(f"Claude Code needs {', '.join(missing)} (set it in .env)")

    def version(self) -> str:
        return VERSION

    def image_for(self, task_image: str) -> str:
        base = image_id(task_image)
        return task_image if base is None else derived_tag(base)

    def prepare_image(self, task_image: str) -> str:
        tag = self.image_for(task_image)
        if tag == task_image:
            return task_image  # not local: starting the world fails as usual (infra_error)
        with _build_lock:
            if image_id(tag) is None:
                with tempfile.TemporaryDirectory() as empty:  # nothing is copied from the host
                    build_image(DOCKERFILE.format(version=VERSION, base=task_image, bin=PACKAGE_BIN), Path(empty), tag)
        return tag

    def start(self, env: Environment, artifacts_dir: Path, limits: Limits, mcp_servers: tuple[McpServer, ...] = ()) -> ClaudeCodeSession:
        if mcp_servers:
            env.write_file(MCP_CONFIG, mcp_config(mcp_servers))
        env_vars = {name: os.environ[name] for name in self.forward_env if name in os.environ} | SETTINGS
        return ClaudeCodeSession(env, self.model.removeprefix("anthropic/"), artifacts_dir, limits, env_vars, bool(mcp_servers))
