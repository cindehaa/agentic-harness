"""The interface every benchmark adapter implements."""

import functools
import hashlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generic, TypeVar

from agentic_harness.agents.base import AgentSession
from agentic_harness.environments.base import Environment
from agentic_harness.types import Score, Task, Transcript, score_to_float

TaskT = TypeVar("TaskT", bound=Task)  # each adapter's own Task subclass


class GraderError(Exception):
    """The grader crashed or its output was unreadable -> outcome grader_error."""


class GraderSetupError(Exception):
    """Preparing the grader failed for an infrastructure reason, e.g. installing its dependencies needs the network
    and there is none -> outcome infra_error (retried). Not a GraderError: the grader itself never ran."""


class Benchmark(ABC, Generic[TaskT]):
    """One benchmark, adapted to the harness. Subclass as e.g. `BashOperations(Benchmark[BashTask])`."""

    name: str  # registry key, e.g. "bash-operations"

    @abstractmethod
    def tasks(self) -> list[TaskT]:
        """Every task in the benchmark."""

    def setup(self, tasks: list[TaskT]) -> None:
        """Optional, once per run before any trial: build images, download data, whatever the benchmark needs.

        tasks() already names each task's image (e.g. "harness-bash-operations:bash-003"); setup() makes sure it exists.
        """

    @abstractmethod
    def run_agent(self, task: TaskT, agent: AgentSession, env: Environment) -> None:
        """Drive the agent's turns. A simple benchmark sends one message; complex ones loop."""

    @abstractmethod
    def grade(self, task: TaskT, env: Environment, transcript: Transcript) -> Score:
        """Judge the result. Grader prep (copy grader files in, install grader deps) goes first."""

    def version(self) -> str | None:
        """What runs.versions records for the benchmark: a content hash of everything that builds its tasks' images
        (adapters also put it in the image tags, so changed files never reuse a stale image). None if not tracked."""
        return None

    def score_to_float(self, score: Score) -> float:
        """The number accuracy averages. Override when this benchmark's Score.value needs its own conversion."""
        return score_to_float(score.value)


@functools.cache
def content_hash(folder: Path, *texts: str) -> str:
    """12 hex chars of sha256 over every file under `folder` (relative path + bytes; caches and dotfiles skipped)
    and `texts` (e.g. the adapter's Dockerfile template). Computed once per process."""
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        parts = path.relative_to(folder).parts
        if path.is_file() and not any(p.startswith(".") or p == "__pycache__" for p in parts):
            digest.update("/".join(parts).encode() + b"\0" + path.read_bytes() + b"\0")
    for text in texts:
        digest.update(text.encode() + b"\0")
    return digest.hexdigest()[:12]
