"""The interface every benchmark adapter implements."""

import functools
import hashlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generic, TypeVar

from agentic_harness.agents.base import AgentCapabilities, AgentSession
from agentic_harness.environments.base import Environment
from agentic_harness.types import Score, Task, Transcript, score_to_float

TaskT = TypeVar("TaskT", bound=Task)  # each adapter's own Task subclass


class GraderError(Exception):
    """The grader crashed or its output was unreadable -> outcome grader_error."""


class Benchmark(ABC, Generic[TaskT]):
    """One benchmark, adapted to the harness. Subclass as e.g. `BashOperations(Benchmark[BashTask])`."""

    name: str  # registry key, e.g. "bash-operations"

    @abstractmethod
    def tasks(self) -> list[TaskT]:
        """Every task in the benchmark."""

    def setup(self, tasks: list[TaskT]) -> None:
        """Optional, once per run before any trial: build or pull the images tasks() names, download data, etc."""

    @abstractmethod
    def run_agent(self, task: TaskT, agent: AgentSession, env: Environment) -> None:
        """Drive the agent's turns. A simple benchmark sends one message; complex ones loop."""

    def requirements(self, task: TaskT) -> AgentCapabilities:
        """What an agent must be able to do for this task; the runner checks it against the agent's capabilities
        before the trial starts. Default: one text message."""
        return AgentCapabilities()

    def grader_image(self, task: TaskT) -> str:
        """The image grade() runs in, attached to the trial's world (sharing its workspace, network and processes).
        Defaults to the task's own image; setup() must make sure it is local."""
        return task.image

    @abstractmethod
    def grade(self, task: TaskT, grader: Environment, env: Environment, transcript: Transcript) -> Score:
        """Judge the result in `grader` (grader files go there, never into the agent's world). `env` is the trial's
        own world, for checks only its own programs can do."""

    def version(self) -> str | None:
        """A content hash of everything that builds the tasks' images, recorded in runs.versions. Adapters also put
        it in the image tags, so changed files never reuse a stale image. None if not tracked."""
        return None

    def score_to_float(self, score: Score) -> float:
        """The number accuracy averages. Override when this benchmark's Score.value needs its own conversion."""
        return score_to_float(score.value)


@functools.cache
def content_hash(folder: Path, *texts: str) -> str:
    """12 hex chars of sha256 over every file under `folder` (relative path + bytes; caches and dotfiles skipped)
    and `texts` (e.g. the adapter's Dockerfile template)."""
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        parts = path.relative_to(folder).parts
        if path.is_file() and not any(p.startswith(".") or p == "__pycache__" for p in parts):
            digest.update("/".join(parts).encode() + b"\0" + path.read_bytes() + b"\0")
    for text in texts:
        digest.update(text.encode() + b"\0")
    return digest.hexdigest()[:12]
