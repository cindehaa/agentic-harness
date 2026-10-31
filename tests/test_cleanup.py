"""`harness cleanup` against real Docker. Its label and image prefix are swapped for test-only ones, so the test can
never remove another run's (or another test's) containers, volumes or cached images."""

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentic_harness import cli as cli_module
from agentic_harness.benchmarks import BENCHMARKS, BashOperations
from agentic_harness.cli import cli
from agentic_harness.environments import ATTEMPT_LABEL, DockerEnvironment, build_image
from agentic_harness.environments import docker as docker_module
from agentic_harness.store import Store

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)

LABEL = "agentic-harness-cleanup-test=1"
PREFIX = "harness-cleanup-test"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "DB_PATH", tmp_path / "harness.db")
    monkeypatch.setattr(docker_module, "LABEL", LABEL)
    monkeypatch.setattr(docker_module, "IMAGE_PREFIX", PREFIX)
    yield Store(tmp_path / "harness.db")
    for kind in ("container", "volume"):  # whatever a failing test left
        names = _listed(kind)
        if names:
            subprocess.run(["docker", kind, "rm", "-f", *names], capture_output=True)


def _listed(kind: str) -> list[str]:
    flags = ["-a", "--format", "{{.Names}}"] if kind == "container" else ["--format", "{{.Name}}"]
    return subprocess.run(["docker", kind, "ls", *flags, "--filter", f"label={LABEL}"], capture_output=True, text=True).stdout.split()


def harness(*args: str) -> str:
    result = CliRunner().invoke(cli, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def _world(attempt: str | None) -> DockerEnvironment:
    env = DockerEnvironment("alpine:latest", labels={ATTEMPT_LABEL: attempt} if attempt else None)
    env.start()
    return env


def test_cleanup_keeps_only_the_worlds_of_live_attempts(store):
    store.create_run("r1", "bash-operations", "mini-swe-agent", "m", {}, {"limits": {}}, ["bash-001"])
    store.claim_setup("w", 120)
    store.finish_setup("r1", "w")
    live = str(store.claim_trial("w", 120).attempt_id)
    kept, _, _ = _world(live), _world("999999"), _world(None)  # the other two: as if their worker died
    kept.attach_grader("alpine:latest").start()  # a live attempt's sidecar is kept with it
    orphan = subprocess.run(["docker", "volume", "create", "--label", LABEL], capture_output=True, text=True).stdout.strip()

    out = harness("cleanup")
    assert "Removed 2 container(s): " in out and "Removed 3 volume(s): " in out and orphan in out
    assert set(_listed("container")) == {n.lstrip("/") for n in _names(kept)} and _listed("volume") == [kept.volume]

    out = harness("cleanup", "--all")
    assert "Removed 2 container(s)" in out and "Removed 1 volume(s)" in out
    assert _listed("container") == [] and _listed("volume") == []


def _names(env: DockerEnvironment) -> list[str]:
    ids = [env.container_id, *(g.container_id for g in env.attached)]
    return subprocess.run(["docker", "inspect", "-f", "{{.Name}}", *ids], capture_output=True, text=True).stdout.split()


class OneImage(BashOperations):
    name = "cleanup-test"

    def tasks(self):
        return [t.__class__(**{**t.__dict__, "image": f"{PREFIX}:needed"}) for t in super().tasks()[:1]]


def test_cleanup_images_removes_only_images_no_unfinished_trial_needs(store, monkeypatch, tmp_path: Path):
    monkeypatch.setitem(BENCHMARKS, OneImage.name, OneImage)
    for tag in ("needed", "old-a", "old-b"):
        build_image(f"FROM alpine:latest\nRUN echo {tag} > /tag\n", tmp_path, f"{PREFIX}:{tag}")
    store.create_run("r1", OneImage.name, "mini-swe-agent", "m", {}, {"limits": {}}, ["bash-001"])  # pending: needs it
    try:
        out = harness("cleanup", "--images")
        assert "Removed 2 image(s), " in out and f"{PREFIX}:old-a" in out and f"{PREFIX}:old-b" in out
        images = subprocess.run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}", PREFIX],
                                capture_output=True, text=True).stdout.split()
        assert images == [f"{PREFIX}:needed"]
        assert "harness-acp-" not in out  # only the test prefix was touched
    finally:
        subprocess.run(["docker", "image", "rm", "-f", f"{PREFIX}:needed", f"{PREFIX}:old-a", f"{PREFIX}:old-b"],
                       capture_output=True)
