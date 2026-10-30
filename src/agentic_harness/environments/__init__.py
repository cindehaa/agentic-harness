"""Environments: where a task runs.

The ports the rest of the harness uses: `Environment` (one world), `EnvironmentFactory` (how the runner gets one,
injected by the worker), and `build_image(dockerfile, context, tag)` (how a benchmark's setup() makes the images its
tasks name: the image-building port). Docker is today's only adapter for all three.
"""

from agentic_harness.environments.base import Environment, EnvironmentFactory, EnvironmentLost, EnvironmentState, ExecResult
from agentic_harness.environments.docker import DockerEnvironment, build_image

__all__ = [
    "DockerEnvironment", "Environment", "EnvironmentFactory", "EnvironmentLost", "EnvironmentState", "ExecResult", "build_image",
]
