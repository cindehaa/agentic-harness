"""What a benchmark sees of an agent. The rest of the agent design (config, limits, multi-turn) comes in Phase 3."""

from abc import ABC, abstractmethod

from agentic_harness.types import Message


class AgentSession(ABC):
    """One live conversation with an agent, as a benchmark sees it.

    The harness wraps the real session to log every turn, build the transcript, and enforce limits.
    """

    @abstractmethod
    def send(self, message: Message) -> Message:
        """Give the agent a message, block until it hands back, and return its reply."""
