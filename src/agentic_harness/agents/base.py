"""What a benchmark sees of an agent (AgentSession), and what the harness starts one from (Agent)."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from agentic_harness.environments.base import Environment
from agentic_harness.types import Limits, Message


class Unsupported(Exception):
    """The agent can't handle this message's content (e.g. a File) -> outcome unsupported."""


class AgentLimitation(Exception):
    """The agent itself couldn't continue (context window exceeded, model refused) -> its turns end and the world
    is graded as it is, like a timeout. Not for our own setup or provider failures (those are infra_error)."""


class AgentSession(ABC):
    """One live conversation with an agent, as a benchmark sees it.

    The harness wraps the real session to log every turn, build the transcript, and enforce limits.
    """

    # Filled in by the agent adapter for the trial's records; benchmarks don't read them
    exit_status: str | None = None  # the agent's own reason for stopping, e.g. mini's "Submitted"
    cost_usd: float = 0.0
    steps: int = 0  # model calls

    @abstractmethod
    def send(self, message: Message) -> Message:
        """Give the agent a message, block until it hands back, and return its reply."""


class Agent(ABC):
    """An agent the harness can run. One instance per run; start() gives each trial a fresh session."""

    name: ClassVar[str]  # registry key, e.g. "mini-swe-agent"

    @abstractmethod
    def start(self, env: Environment, artifacts_dir: Path, limits: Limits) -> AgentSession:
        """A new session that acts only through `env`, saves its own files (e.g. a trajectory) in `artifacts_dir`,
        and stops itself within `limits`."""
