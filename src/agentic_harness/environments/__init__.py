"""Environments: where a task runs."""

from agentic_harness.environments.base import Environment, ExecResult
from agentic_harness.environments.docker import DockerEnvironment, build_image

__all__ = ["DockerEnvironment", "Environment", "ExecResult", "build_image"]
