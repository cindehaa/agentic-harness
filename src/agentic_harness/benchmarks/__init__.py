"""Benchmarks: adapters that plug a benchmark into the harness."""

from agentic_harness.benchmarks.base import Benchmark, BenchmarkUnavailable, GraderError
from agentic_harness.benchmarks.bash_operations import BashOperations
from agentic_harness.benchmarks.python_tasks import PythonTasks
from agentic_harness.benchmarks.terminal_bench import TerminalBench

BENCHMARKS: dict[str, type[Benchmark]] = {
    "bash-operations": BashOperations,
    "python-tasks": PythonTasks,
    "terminal-bench": TerminalBench,
}

__all__ = ["BENCHMARKS", "BashOperations", "Benchmark", "BenchmarkUnavailable", "GraderError", "PythonTasks", "TerminalBench"]
