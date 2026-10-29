"""Benchmarks: adapters that plug a benchmark into the harness."""

from agentic_harness.benchmarks.base import Benchmark, GraderError
from agentic_harness.benchmarks.bash_operations import BashOperations

BENCHMARKS: dict[str, type[Benchmark]] = {"bash-operations": BashOperations}  # registry: name -> adapter class

__all__ = ["BENCHMARKS", "BashOperations", "Benchmark", "GraderError"]
