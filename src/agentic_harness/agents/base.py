"""What a benchmark sees of an agent (AgentSession), and what the harness starts one from (Agent)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agentic_harness.environments.base import Environment
from agentic_harness.types import Limits, Message


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
    steps: int = 0  # model calls


class AgentSession(ABC):
    """One live conversation with an agent, as a benchmark sees it (the harness wraps it to record and cap turns)."""

    @abstractmethod
    def send(self, message: Message) -> Message:
        """Give the agent a message, block until it hands back, and return its reply."""

    def stats(self) -> AgentStats:
        """Stats so far, including after a crash. A session that tracks nothing reports zeros."""
        return AgentStats()


class Agent(ABC):
    """An agent the harness can run. One instance per run; start() gives each trial a fresh session."""

    name: ClassVar[str]  # registry key, e.g. "mini-swe-agent"
    model_env: ClassVar[str | None] = None  # the environment variable with its default model, e.g. MSWEA_MODEL_NAME
    capabilities: ClassVar[AgentCapabilities] = AgentCapabilities()  # the least: one text message

    def __init__(self, model: str) -> None:
        self.model = model  # resolved at submit time (runs.model); adapters never read model_env themselves

    @abstractmethod
    def start(self, env: Environment, artifacts_dir: Path, limits: Limits) -> AgentSession:
        """A new session that acts only through `env`, saves its own files (e.g. a trajectory) in `artifacts_dir`,
        and stops itself within `limits`."""

    def preflight(self) -> None:
        """Raise PreflightError if this agent can't run at all. Must be cheap and spend no model tokens."""

    def version(self) -> str | None:
        """What runs.versions records for the agent, e.g. its code's git commit. None if unknown."""
        return None
