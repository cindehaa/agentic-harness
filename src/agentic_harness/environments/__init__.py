"""Environments: where a task runs.

The ports the rest of the harness uses: `Environment` (one world), `EnvironmentFactory` (how the runner gets one,
injected by the worker), and `build_image(dockerfile, context, tag)` + `ensure_image(image)` (how a benchmark's
setup() makes or pulls the images its tasks and graders name: the image port). Docker is today's only adapter.
"""

from agentic_harness.environments.base import (
    EndReason, Environment, EnvironmentFactory, EnvironmentLost, EnvironmentState, ExecResult,
)
from agentic_harness.environments.docker import DockerEnvironment, build_image, ensure_image

__all__ = [
    "DockerEnvironment", "EndReason", "Environment", "EnvironmentFactory", "EnvironmentLost", "EnvironmentState", "ExecResult", "build_image", "ensure_image",
]
