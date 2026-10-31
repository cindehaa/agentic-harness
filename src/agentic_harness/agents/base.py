from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agentic_harness.environments.base import Environment
from agentic_harness.types import Limits, McpServer, Message

MODEL_ENV = "HARNESS_MODEL"


class Unsupported(Exception):
    """The agent can't handle this message's content -> outcome unsupported."""


class AgentLimitation(Exception):
    """The agent can't go on (context full, model refused): its world is graded as is."""


class TransientError(Exception):
    """A temporary failure outside the agent, after its own retries -> infra_error, retried."""


class PreflightError(Exception):
    """The agent can't run at all (unknown model, missing API key); checked before a run."""


@dataclass(frozen=True)
class AgentCapabilities:
    content_types: frozenset[str] = frozenset({"text"})
    multi_turn: bool = False
    mcp: bool = False

    def missing(self, needed: "AgentCapabilities") -> list[str]:
        gaps = [f"content: {t}" for t in sorted(needed.content_types - self.content_types)]
        if needed.multi_turn and not self.multi_turn:
            gaps.append("multi_turn")
        if needed.mcp and not self.mcp:
            gaps.append("mcp")
        return gaps


@dataclass(frozen=True)
class AgentStats:
    exit_status: str | None = None
    cost_usd: float = 0.0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class AgentSession(ABC):
    @abstractmethod
    def send(self, message: Message) -> Message:
        """Give the agent a message, block until it hands back, and return its reply."""

    def stats(self) -> AgentStats:
        """Stats so far, also after a crash or timeout: money spent is always reported."""
        return AgentStats()

    def new_events(self) -> list[tuple[str, dict]]:
        """Trace events the agent recorded itself since the last call."""
        return []


class Agent(ABC):
    name: ClassVar[str]
    capabilities: ClassVar[AgentCapabilities] = AgentCapabilities()
    forward_env: ClassVar[tuple[str, ...]] = ()

    def __init__(self, model: str) -> None:
        self.model = model

    @abstractmethod
    def start(self, env: Environment, artifacts_dir: Path, limits: Limits, mcp_servers: tuple[McpServer, ...] = ()) -> AgentSession:
        """A new session acting only through `env`; it stops itself within `limits`."""

    def prepare_image(self, task_image: str) -> str:
        """The image a trial's world starts from; default: the task's own."""
        return task_image

    def image_for(self, task_image: str) -> str:
        """What prepare_image() returns for `task_image`, without building anything."""
        return task_image

    def native_model(self) -> str:
        """`self.model` in the form the agent's own program takes."""
        return self.model

    def unenforced_limits(self) -> frozenset[str]:
        """Limits fields this agent can't keep; the attempt records them as not enforced."""
        return frozenset()

    def preflight(self) -> None:
        """Raise PreflightError if this agent can't run at all. Spends no model tokens."""

    def version(self) -> str | None:
        """What runs.versions records for the agent; None if unknown."""
        return None
