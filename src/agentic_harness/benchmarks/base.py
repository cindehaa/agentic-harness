"""The interface every benchmark adapter implements."""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from agentic_harness.agents.base import AgentSession
from agentic_harness.environments.base import Environment
from agentic_harness.types import Score, Task, Transcript

TaskT = TypeVar("TaskT", bound=Task)  # each adapter's own Task subclass


class Benchmark(ABC, Generic[TaskT]):
    """One benchmark, adapted to the harness. Subclass as e.g. `BashOperations(Benchmark[BashTask])`."""

    name: str  # registry key, e.g. "bash-operations"

    @abstractmethod
    def tasks(self) -> list[TaskT]:
        """Every task in the benchmark."""

    @abstractmethod
    def build_world(self, task: TaskT, env: Environment) -> None:
        """Prepare the started environment for this task (files, setup commands)."""

    @abstractmethod
    def run_agent(self, task: TaskT, agent: AgentSession, env: Environment) -> None:
        """Drive the agent's turns. A simple benchmark sends one message; complex ones loop."""

    @abstractmethod
    def grade(self, task: TaskT, env: Environment, transcript: Transcript) -> Score:
        """Judge the result. Grader prep (install deps, copy grader files, restore tests) goes first."""
