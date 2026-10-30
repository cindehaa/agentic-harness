"""Benchmarks: adapters that plug a benchmark into the harness."""

from agentic_harness.benchmarks.base import Benchmark, GraderError, GraderSetupError
from agentic_harness.benchmarks.bash_operations import BashOperations
from agentic_harness.benchmarks.python_tasks import PythonTasks

# registry: name -> adapter class
BENCHMARKS: dict[str, type[Benchmark]] = {
    "bash-operations": BashOperations,
    "python-tasks": PythonTasks,
}

__all__ = ["BENCHMARKS", "BashOperations", "Benchmark", "GraderError", "GraderSetupError", "PythonTasks"]
