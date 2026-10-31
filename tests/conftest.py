import uuid

import pytest

from agentic_harness.environments import docker as docker_module


@pytest.fixture(scope="module", autouse=True)
def own_docker_label():
    real = docker_module.LABEL
    docker_module.LABEL = f"agentic-harness.test={uuid.uuid4().hex[:12]}"
    yield
    docker_module.LABEL = real
