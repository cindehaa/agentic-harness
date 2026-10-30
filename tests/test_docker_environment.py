"""DockerEnvironment against a real Docker daemon (skipped if Docker isn't running)."""

import json
import subprocess
import time

import pytest

from agentic_harness.environments import DockerEnvironment, EnvironmentLost, EnvironmentState
from agentic_harness.environments.docker import MAX_OUTPUT, DockerError
from agentic_harness.types import Limits

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
    env.upload(src, "/workspace/up")  # again, onto the existing folder: replaces, doesn't nest
    assert env.read_file("/workspace/up/sub/f.txt") == b"data"
    env.download("/workspace/up", tmp_path / "out" / "back")
    assert (tmp_path / "out" / "back" / "sub" / "f.txt").read_text() == "data"
    assert env.exec("ls /workspace/up").stdout == "sub\n"


def test_stop_twice_is_safe():
    env = DockerEnvironment(IMAGE)
    env.stop()  # before start
    env.start()
    env.stop()
    env.stop()
    assert env.container_id is None


def _listed(container_id: str) -> str:
    return subprocess.run(
        ["docker", "ps", "-a", "-q", "--no-trunc", "--filter", f"id={container_id}"], capture_output=True, text=True
    ).stdout


def test_exec_when_container_is_gone():
    with DockerEnvironment(IMAGE) as env:
        subprocess.run(["docker", "rm", "-f", env.container_id], capture_output=True, check=True)
        with pytest.raises(DockerError, match="container is gone"):
            env.exec("true")


def test_exec_with_missing_cwd_is_a_normal_failure(env):
    # the agent can delete its own cwd, so this must not become an (excluded) infra error
    result = env.exec("true", cwd="/no/such/dir")
    assert result.exit_code != 0
    assert "/no/such/dir" in result.stdout + result.stderr


def test_fake_docker_error_from_a_command_is_a_normal_result(env):
    result = env.exec("echo 'OCI runtime exec failed: exec failed: chdir to cwd: no such file' >&2; exit 126")
    assert result.exit_code == 126
    assert "OCI runtime exec failed" in result.stderr


def test_exit_127_from_a_command_is_a_normal_result(env):
    assert env.exec("nosuchprogram").exit_code == 127
    assert env.exec("exit 127").exit_code == 127


def test_upload_replaces_planted_folder(env, tmp_path):
    env.exec("mkdir -p /grader/tests && echo evil > /grader/tests/planted.txt")
    (tmp_path / "t.txt").write_text("real")
    env.upload(tmp_path, "/grader/tests")
    assert env.exec("ls -A /grader/tests").stdout == "t.txt\n"


def test_upload_single_file(env, tmp_path):
    (tmp_path / "one.txt").write_text("1")
    env.upload(tmp_path / "one.txt", "files/one.txt")
    assert env.read_file("files/one.txt") == b"1"


def test_download_refuses_existing_local_path(env, tmp_path):
    env.write_file("d.txt", "x")
    (tmp_path / "d.txt").write_text("host")
    with pytest.raises(FileExistsError):
        env.download("d.txt", tmp_path / "d.txt")
    assert (tmp_path / "d.txt").read_text() == "host"


def test_binary_round_trip(env):
    data = bytes(range(256)) + b"\xff\xfe\x00"
    env.write_file("bin.dat", data)
    assert env.read_file("bin.dat") == data


def test_read_directory_raises_oserror(env):
    with pytest.raises(OSError) as info:
        env.read_file("/workspace")
    assert not isinstance(info.value, FileNotFoundError)


def test_output_is_truncated(env):
    result = env.exec("head -c 3000000 /dev/zero | tr '\\0' a")
    assert result.exit_code == 0 and result.truncated
    assert len(result.stdout) <= MAX_OUTPUT


def test_image_with_entrypoint_starts():
    tag = "harness-test-entrypoint"
    dockerfile = b'FROM alpine:latest\nENTRYPOINT ["echo"]\n'
    subprocess.run(["docker", "build", "-t", tag, "-"], input=dockerfile, capture_output=True, check=True)
    try:
        with DockerEnvironment(tag) as env:
            assert env.exec("echo ok").stdout == "ok\n"
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


def test_container_is_gone_after_stop():
    env = DockerEnvironment(IMAGE)
    env.start()
    container_id = env.container_id
    env.stop()
    assert _listed(container_id) == ""


def test_state_of_running_container(env):
    assert env.state() == EnvironmentState(running=True, exit_code=0, oom_killed=False)


def test_state_after_main_process_dies():
    with DockerEnvironment(IMAGE) as env:
        env.exec("kill 1")  # the init forwards SIGTERM to the sleep; the container exits but is kept (no --rm)
        time.sleep(1)
        state = env.state()
    assert not state.running and state.exit_code != 0 and not state.oom_killed


def test_state_after_removal_is_none():
    with DockerEnvironment(IMAGE) as env:
        subprocess.run(["docker", "rm", "-f", env.container_id], capture_output=True, check=True)
        assert env.state() is None


def test_kill_agent_processes_keeps_container_alive():
    with DockerEnvironment(IMAGE) as env:
        env.exec("sleep 999 > /dev/null 2>&1 &")
        assert "sleep 999" in env.exec("ps").stdout
        env.kill_agent_processes()
        assert "sleep 999" not in env.exec("ps").stdout
        assert env.state().running


def test_docker_error_is_the_backend_neutral_environment_lost():
    assert issubclass(DockerError, EnvironmentLost)  # the runner only knows EnvironmentLost


def test_state_after_lifetime_expires_is_exit_code_0():
    with DockerEnvironment(IMAGE, lifetime_s=1) as env:
        while env.state().running:
            time.sleep(0.2)
        assert env.state().exit_code == 0  # how the runner tells "lifetime ran out" from "killed"


def test_resource_limits_are_applied():
    with DockerEnvironment(IMAGE, limits=Limits(memory="256m", cpus=0.5, pids_limit=64)) as env:
        host = json.loads(subprocess.run(["docker", "inspect", "-f", "{{json .HostConfig}}", env.container_id],
                                         capture_output=True, check=True).stdout)
    assert (host["Memory"], host["NanoCpus"], host["PidsLimit"]) == (256 * 2**20, 500_000_000, 64)
