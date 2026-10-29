"""Shared data types for the agentic harness."""

from dataclasses import dataclass, field
from typing import Any, Literal

# What a grader can return: a verdict, a number, or named results
Value = Any # must be JSON-serializable (the recorder checks); e.g. True, 0.75, "C", {"passed": True}


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


@dataclass(frozen=True)
class Data:
    data: Any  # any JSON-serializable value; note: frozen stops reassignment, not mutation of a dict inside


Content = Text | File | Data


@dataclass(frozen=True)
class Message:
    """One message exchanged between the benchmark and the agent. Benchmarks send "user" messages; agents reply as "agent"."""

    role: Literal["user", "agent"]
    content: tuple[Content, ...]


# Everything said during a trial's turns: benchmark messages ("user") and agent replies ("agent").
# The session builds it as a list while the agent runs and hands grade() a read-only snapshot.
# The agent's individual commands/tool calls live in the recorder's event log, not here.
Transcript = tuple[Message, ...]


@dataclass(frozen=True)
class Task:
    """One task from a benchmark. Adapters subclass this with their own typed fields.

    Base fields deliberately have no defaults: if they did, every subclass field would
    need a default too (dataclass inheritance rule).
    """

    benchmark: str  # e.g. "bash-operations"; makes a Task self-describing in logs and the DB
    id: str  # e.g. "bash-001"
    image: str  # Docker image for the world (prebuilt by the benchmark, or built in Benchmark.setup())


@dataclass(frozen=True)
class Limits:
    """What one trial may spend. Defaults decided 2026-10-29 (`ideation/agents-and-runner.md`)."""

    trial_timeout_s: float = 600  # the first agent command after this deadline ends the agent's run
    cost_usd: float = 1.0  # model spend per trial
    max_steps: int = 50  # model calls per trial
    command_timeout_s: float = 60  # one agent command
