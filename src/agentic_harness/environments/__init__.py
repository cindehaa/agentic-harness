"""Environments: where a task runs. Docker is the only backend."""

from agentic_harness.environments.base import (
    EndReason, Environment, EnvironmentFactory, EnvironmentLost, EnvironmentState, ExecResult, Sidecar,
)
from agentic_harness.environments.docker import (
    ATTEMPT_LABEL, DockerEnvironment, build_image, ensure_image, image_id, remove_images, remove_leftovers,
)

__all__ = [
    "DockerEnvironment", "EndReason", "Environment", "EnvironmentFactory", "EnvironmentLost", "EnvironmentState", "ExecResult", "Sidecar", "build_image", "ensure_image", "image_id", "remove_images", "remove_leftovers", "ATTEMPT_LABEL",
]
