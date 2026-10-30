"""Environments: where a task runs. Docker is the only backend."""

from agentic_harness.environments.base import (
    EndReason, Environment, EnvironmentFactory, EnvironmentLost, EnvironmentState, ExecResult, Sidecar,
)
from agentic_harness.environments.docker import DockerEnvironment, build_image, ensure_image

__all__ = [
    "DockerEnvironment", "EndReason", "Environment", "EnvironmentFactory", "EnvironmentLost", "EnvironmentState", "ExecResult", "Sidecar", "build_image", "ensure_image",
]
