"""Shared data types for the agentic harness."""

from dataclasses import dataclass, field
from typing import Any, Literal

Value = Any  # what a grader returns; must be JSON-serializable, e.g. True, 0.75, "C", {"passed": True}


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
    data: Any  # any JSON-serializable value (frozen stops reassignment, not mutation of a dict inside)


Content = Text | File | Data


@dataclass(frozen=True)
class Message:
    """One message exchanged between the benchmark and the agent. Benchmarks send "user" messages; agents reply as "agent"."""

    role: Literal["user", "agent"]
    content: tuple[Content, ...]


# Every message of a trial's turns, in order; grade() gets a read-only snapshot.
# The agent's individual commands are in events.jsonl, not here.
Transcript = tuple[Message, ...]


@dataclass(frozen=True)
class Task:
    """One task from a benchmark. Adapters subclass it with their own fields, so these have no defaults (otherwise
    every subclass field would need one too)."""

    benchmark: str  # e.g. "bash-operations"; makes a Task self-describing in logs and the DB
    id: str  # e.g. "bash-001"
    image: str  # Docker image for the world (prebuilt by the benchmark, or built in Benchmark.setup())


@dataclass(frozen=True)
class Limits:
    """What one trial may spend."""

    trial_timeout_s: float = 600  # the first agent command after this deadline ends the agent's run
    cost_usd: float = 1.0  # model spend per trial
    max_steps: int = 50  # model calls per trial
    command_timeout_s: float = 60  # one agent command
    max_turns: int = 20  # benchmark -> agent messages per trial (a looping benchmark can't run forever)
    # per container (docker run --memory / --cpus / --pids-limit); going over the memory limit fails the trial
    memory: str = "2g"
    cpus: float = 1.0
    pids_limit: int = 512  # processes; stops a fork bomb from taking the host down


@dataclass(frozen=True)
class Outcome:
    """How one attempt at one trial ended. Accuracy counts only "graded"."""

    status: Literal["graded", "grader_error", "infra_error", "unsupported"]
    score: Score | None = None
    score_float: float | None = None  # what accuracy averages; from Benchmark.score_to_float
    error: str | None = None  # why it isn't graded (or the traceback)
    retryable: bool = False  # infra_error only: a transient failure (Docker, network, provider overloaded) worth another attempt
    agent_exit_status: str | None = None  # the agent's own reason for stopping, e.g. "Submitted", "TimeLimit"
    cost_usd: float = 0.0
    duration_s: float = 0.0
    turns: int = 0


def score_to_float(value: Value) -> float:
    """Inspect-style default: True/"C" -> 1, "P" (partial) -> 0.5, False/"I"/"N" -> 0, numbers as they are."""
    if value is True or value == "C":
        return 1.0
    if value == "P":
        return 0.5
    if value is False or value == "I" or value == "N":
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f"no default conversion to a number for score value {value!r}")
