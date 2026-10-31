"""What a benchmark sees of an agent (AgentSession), and what the harness starts one from (Agent)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agentic_harness.environments.base import Environment
from agentic_harness.types import Limits, McpServer, Message

# The one model setting for every agent (in .env; --model overrides it): a litellm-style id such as
# anthropic/claude-haiku-5-5. Each agent translates it into its own program's form (Agent.native_model).
MODEL_ENV = "HARNESS_MODEL"


class Unsupported(Exception):
    """The agent can't handle this message's content (e.g. a File) -> outcome unsupported."""


class AgentLimitation(Exception):
    """The agent itself couldn't continue (context window exceeded, model refused) -> its turns end and the world
    is graded as it is, like a timeout. Not for our own setup or provider failures (those are infra_error)."""


class TransientError(Exception):
    """A temporary failure outside the agent (provider overloaded, 5xx, rate limited, network down) -> infra_error,
    retried. Raise it only after the agent's own retries; other errors (auth, bad request, a bug) are not retried."""


class PreflightError(Exception):
    """The agent can't run at all (unknown model, missing API key): checked before a run is submitted or a worker
    starts, so it fails once, loudly, instead of in every trial."""


@dataclass(frozen=True)
class AgentCapabilities:
    """What an agent can do. Agents declare theirs; benchmarks say what a task needs (Benchmark.requirements); the
    runner compares the two once, before a trial starts, and a gap makes the trial unsupported."""

    content_types: frozenset[str] = frozenset({"text"})  # message content it reads: "text", "file", "data"
    multi_turn: bool = False  # a second send() continues the same conversation
    mcp: bool = False  # uses tools from the MCP servers a benchmark declares

    def missing(self, needed: "AgentCapabilities") -> list[str]:
        """What `needed` asks for that this agent can't do, e.g. ["content: file", "mcp"]. Empty if nothing."""
        gaps = [f"content: {t}" for t in sorted(needed.content_types - self.content_types)]
        if needed.multi_turn and not self.multi_turn:
            gaps.append("multi_turn")
        if needed.mcp and not self.mcp:
            gaps.append("mcp")
        return gaps


@dataclass(frozen=True)
class AgentStats:
    """What the session reports for the trial's records; benchmarks don't read it."""

    exit_status: str | None = None  # the agent's own reason for stopping, e.g. mini's "Submitted"
    cost_usd: float = 0.0
    model_calls: int = 0
    input_tokens: int = 0  # every prompt token, cached ones included
    output_tokens: int = 0


class AgentSession(ABC):
    """One live conversation with an agent, as a benchmark sees it (the harness wraps it to record and cap turns)."""

    @abstractmethod
    def send(self, message: Message) -> Message:
        """Give the agent a message, block until it hands back, and return its reply."""

    def stats(self) -> AgentStats:
        """Stats so far, including after a crash or a timeout: money already spent is always reported. A session that
        tracks nothing reports zeros."""
        return AgentStats()

    def new_events(self) -> list[tuple[str, dict]]:
        """Trace events (kind, payload) the agent recorded itself since the last call, e.g. the tool calls of an agent
        that runs inside the world, whose commands never pass through our exec. The harness writes them to the trace
        after each turn. Default: none (the recording environment already traces every command)."""
        return []


class Agent(ABC):
    """An agent the harness can run. One instance per run; start() gives each trial a fresh session."""

    name: ClassVar[str]  # registry key, e.g. "mini-swe-agent"
    capabilities: ClassVar[AgentCapabilities] = AgentCapabilities()  # the least: one text message
    forward_env: ClassVar[tuple[str, ...]] = ()  # host variables (e.g. its API key) it passes into the world; redacted

    def __init__(self, model: str) -> None:
        self.model = model  # litellm-style, resolved at submit time (runs.model); adapters never read the environment for it

    @abstractmethod
    def start(self, env: Environment, artifacts_dir: Path, limits: Limits, mcp_servers: tuple[McpServer, ...] = ()) -> AgentSession:
        """A new session that acts only through `env`, saves its own files (e.g. a trajectory) in `artifacts_dir`,
        and stops itself within `limits`. `mcp_servers` are already running, reachable at their `url` from inside
        `env` (only an agent with capabilities.mcp gets any)."""

    def prepare_image(self, task_image: str) -> str:
        """The image a trial's world starts from. Default: the task's own. An agent that must run inside the world
        (e.g. Claude Code) returns a derived image: the task's image plus the agent's own program, built once and
        cached. The runner calls it right before starting the world; graders still use their own image."""
        return task_image

    def image_for(self, task_image: str) -> str:
        """The image prepare_image() returns for `task_image`, without building anything (`harness cleanup --images`
        keeps it while a trial still needs it)."""
        return task_image

    def native_model(self) -> str:
        """`self.model` in the form the agent's own program takes. Default: as is (litellm-style)."""
        return self.model

    def unenforced_limits(self) -> frozenset[str]:
        """Limits fields this agent can't keep (e.g. "max_steps" for one that doesn't count its model calls). The
        attempt records them as not enforced rather than pretending. Default: none."""
        return frozenset()

    def preflight(self) -> None:
        """Raise PreflightError if this agent can't run at all. Must be cheap and spend no model tokens."""

    def version(self) -> str | None:
        """What runs.versions records for the agent, e.g. its code's git commit. None if unknown."""
        return None
