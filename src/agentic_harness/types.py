"""Shared data types for the agentic harness."""

from dataclasses import dataclass, field
from typing import Any

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
