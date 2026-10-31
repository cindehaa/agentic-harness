import functools
import hashlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generic, TypeVar

from agentic_harness.agents.base import AgentCapabilities, AgentSession
from agentic_harness.environments.base import Environment
from agentic_harness.types import LimitOverrides, McpServer, OriginalResult, ReferenceSolution, Score, Task, Transcript, score_to_float

TaskT = TypeVar("TaskT", bound=Task)


class GraderError(Exception):
    """The grader crashed or its output was unreadable -> outcome grader_error."""


class BenchmarkUnavailable(Exception):
    ...


class Benchmark(ABC, Generic[TaskT]):
    name: str

    @abstractmethod
    def tasks(self) -> list[TaskT]:
        """Every task in the benchmark."""

    def setup(self, tasks: list[TaskT]) -> None:
        """Once per run, before any trial: build or pull the images, download data."""

    @abstractmethod
    def run_agent(self, task: TaskT, agent: AgentSession, env: Environment) -> None:
        """Drive the agent's turns: one message for a simple benchmark, a loop otherwise."""

    def requirements(self, task: TaskT) -> AgentCapabilities:
        """What an agent must be able to do for this task."""
        return AgentCapabilities(mcp=bool(self.mcp_servers(task)))

    def mcp_servers(self, task: TaskT) -> list[McpServer]:
        """MCP servers for this task's actions; their images must be local (setup())."""
        return []

    def limits(self, task: TaskT) -> LimitOverrides:
        """The limits this benchmark declares for the task; explicit run flags override them."""
        return LimitOverrides()

    def grader_image(self, task: TaskT) -> str:
        """The image grade() runs in, attached to the world; setup() must make it local."""
        return task.image

    @abstractmethod
    def grade(self, task: TaskT, grader: Environment, env: Environment, transcript: Transcript) -> Score:
        """Judge the result in `grader`; grader files never go into the agent's world."""

    def reference_solution(self, task: TaskT) -> ReferenceSolution | None:
        """A known-good answer `harness validate` applies; prefer the benchmark's own."""
        return None

    def original_workflow(self, task: TaskT, agent_name: str, model: str, out_dir: Path) -> OriginalResult:
        """Run `task` the benchmark's original way, for `harness parity`."""
        raise NotImplementedError(f"{self.name} declares no original workflow")

    def version(self) -> str | None:
        """A content hash of everything that builds the tasks' images; None if untracked."""
        return None

    def score_to_float(self, score: Score) -> float:
        """The number accuracy averages."""
        return score_to_float(score.value)


@functools.cache
def content_hash(folder: Path, *texts: str) -> str:
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        parts = path.relative_to(folder).parts
        if path.is_file() and not any(p.startswith(".") or p == "__pycache__" for p in parts):
            digest.update("/".join(parts).encode() + b"\0" + path.read_bytes() + b"\0")
    for text in texts:
        digest.update(text.encode() + b"\0")
    return digest.hexdigest()[:12]
