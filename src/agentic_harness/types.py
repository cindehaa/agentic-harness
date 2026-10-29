"""Shared data types for the agentic harness."""

from dataclasses import dataclass, field
from typing import Any, Literal

# What a grader can return: a verdict, a number, or named results (mirrors Inspect AI's Value).
Value = (
    str | int | float | bool
    | list[str | int | float | bool]
    | dict[str, str | int | float | bool | None]
)


@dataclass
class Score:
    """A benchmark's verdict on one trial."""

    value: Value
    explanation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# --- Message content: the pieces a message is made of (A2A calls these parts) ---


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class File:
    path: str  # where the file lives in the world (inside the container), e.g. "/workspace/shot.png"
    media_type: str  # MIME type, e.g. "image/png", "application/pdf"
    inline: bool = False  # benchmark requires the agent to see it directly in the message


@dataclass(frozen=True)
class Data:
    data: Any  # any JSON-serializable value; note: frozen stops reassignment, not mutation of a dict inside


Content = Text | File | Data


@dataclass(frozen=True)
class Message:
    """One message in an episode. Benchmarks send "user" messages; agents reply as "agent"."""

    role: Literal["user", "agent"]
    content: tuple[Content, ...]
