"""DockerEnvironment against a real Docker daemon (skipped if Docker isn't running)."""

import subprocess
import time

import pytest

from agentic_harness.environments import DockerEnvironment
from agentic_harness.environments.docker import DockerError

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)

IMAGE = "alpine:latest"


@pytest.fixture(scope="module")
def env():
    with DockerEnvironment(IMAGE) as env:
        yield env


def test_context_manager_starts_and_removes_container():
    with DockerEnvironment(IMAGE) as env:
        container_id = env.container_id
        assert env.exec("true").exit_code == 0
    listed = subprocess.run(
        ["docker", "ps", "-a", "-q", "--no-trunc", "--filter", "label=agentic-harness=1"],
        capture_output=True, text=True,
    ).stdout
    assert container_id not in listed


def test_default_cwd_is_workspace(env):
    assert env.exec("pwd").stdout.strip() == "/workspace"


def test_string_runs_through_shell(env):
    assert env.exec("echo a && echo b").stdout == "a\nb\n"


def test_list_runs_directly(env):
    assert env.exec(["echo", "a && echo b"]).stdout == "a && echo b\n"


def test_stdout_stderr_and_exit_code(env):
    result = env.exec(["sh", "-c", "echo out; echo err >&2; exit 3"])
    assert (result.exit_code, result.stdout, result.stderr) == (3, "out\n", "err\n")


def test_timeout(env):
    began = time.monotonic()
    result = env.exec("sleep 5", timeout=1)
    assert result.timed_out and result.exit_code == -1
    assert time.monotonic() - began < 4


def test_write_then_read_file(env):
    env.write_file("a/b/c.txt", "hello")
    assert env.read_file("a/b/c.txt") == b"hello"
    assert env.read_file("/workspace/a/b/c.txt") == b"hello"


def test_read_missing_file(env):
    with pytest.raises(FileNotFoundError):
        env.read_file("/nope.txt")


def test_read_file_when_container_is_gone():
    with DockerEnvironment(IMAGE) as env:
        subprocess.run(["docker", "rm", "-f", env.container_id], capture_output=True, check=True)
        with pytest.raises(DockerError, match="container is gone"):
            env.read_file("/etc/hostname")


def test_upload_and_download_folder(env, tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_text("data")
    env.upload(src, "/workspace/up")
    assert env.read_file("/workspace/up/sub/f.txt") == b"data"
    env.download("/workspace/up", tmp_path / "out" / "back")
    assert (tmp_path / "out" / "back" / "sub" / "f.txt").read_text() == "data"


def test_stop_twice_is_safe():
    env = DockerEnvironment(IMAGE)
    env.stop()  # before start
    env.start()
    env.stop()
    env.stop()
    assert env.container_id is None
