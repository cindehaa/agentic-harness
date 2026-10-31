import json
import subprocess
import tarfile
import time

import pytest

from agentic_harness.environments import DockerEnvironment, EndReason, EnvironmentLost, EnvironmentState
from agentic_harness.environments import docker as docker_module
from agentic_harness.environments.docker import MAX_OUTPUT, DockerError, _end_reason
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
        ["docker", "ps", "-a", "-q", "--no-trunc", "--filter", f"label={docker_module.LABEL}"],
        capture_output=True, text=True,
    ).stdout
    assert container_id not in listed


def test_default_cwd_is_workspace(env):
    assert env.exec("pwd").stdout.strip() == "/workspace"


def test_started_process_talks_through_pipes_in_the_workdir_with_its_variables(env, monkeypatch):
    monkeypatch.setenv("SOME_KEY", "host-value")
    proc = env.start_process(["sh", "-c", 'pwd; echo "$SOME_KEY"; read line; echo "got $line"'], env={"SOME_KEY": "v"})
    out, _ = proc.communicate(b"hello\n", timeout=30)
    assert out.decode().splitlines() == ["/workspace", "v", "got hello"]


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
    env.upload(src, "/workspace/up")
    assert env.read_file("/workspace/up/sub/f.txt") == b"data"
    env.download("/workspace/up", tmp_path / "out" / "back")
    assert (tmp_path / "out" / "back" / "sub" / "f.txt").read_text() == "data"
    assert env.exec("ls /workspace/up").stdout == "sub\n"


def test_stop_twice_is_safe():
    env = DockerEnvironment(IMAGE)
    env.stop()
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
    assert env.state() == EnvironmentState(running=True)


def test_state_after_main_process_dies():
    with DockerEnvironment(IMAGE) as env:
        env.exec("kill 1")
        time.sleep(1)
        state = env.state()
    assert state == EnvironmentState(running=False, end_reason=EndReason.KILLED)


@pytest.mark.parametrize("status, exit_code, oom_killed, reason", [
    ("exited", 0, False, EndReason.EXPIRED),
    ("exited", 143, False, EndReason.KILLED),
    ("exited", 137, True, EndReason.RESOURCE_LIMIT),
    ("dead", 0, False, EndReason.UNKNOWN),
])
def test_docker_evidence_becomes_an_end_reason(status, exit_code, oom_killed, reason):
    assert _end_reason({"Status": status, "ExitCode": exit_code, "OOMKilled": oom_killed}) is reason


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


def test_kill_agent_processes_outlasts_a_fork_loop():
    with DockerEnvironment(IMAGE, limits=Limits(pids_limit=4096)) as env:
        env.exec("(while :; do sleep 0.5 & done) > /dev/null 2>&1 &")
        time.sleep(1)
        env.kill_agent_processes()
        assert "sleep 0.5" not in env.exec("ps -o args").stdout
        assert env.state().running


def test_docker_error_is_the_backend_neutral_environment_lost():
    assert issubclass(DockerError, EnvironmentLost)


def test_state_after_lifetime_expires_is_exit_code_0():
    with DockerEnvironment(IMAGE, lifetime_s=1) as env:
        while env.state().running:
            time.sleep(0.2)
        assert env.state().end_reason is EndReason.EXPIRED


def test_resource_limits_are_applied():
    with DockerEnvironment(IMAGE, limits=Limits(memory="256m", cpus=0.5, pids_limit=64)) as env:
        host = json.loads(subprocess.run(["docker", "inspect", "-f", "{{json .HostConfig}}", env.container_id],
                                         capture_output=True, check=True).stdout)
    assert (host["Memory"], host["NanoCpus"], host["PidsLimit"]) == (256 * 2**20, 500_000_000, 64)


def test_save_workspace_is_a_tar_that_keeps_symlinks_as_links(env, tmp_path):
    env.exec("echo hi > a.txt && ln -s /etc/passwd link")
    assert env.save_workspace(tmp_path / "w.tar", 10_000_000) is False
    with tarfile.open(tmp_path / "w.tar") as tar:
        members = {m.name.removeprefix("./"): m for m in tar.getmembers()}
        assert tar.extractfile(members["a.txt"]).read() == b"hi\n"
        assert members["link"].issym() and members["link"].linkname == "/etc/passwd"


def test_save_workspace_stops_at_the_cap(env, tmp_path):
    env.exec("head -c 1000000 /dev/zero > big")
    assert env.save_workspace(tmp_path / "w.tar", 100_000) is True
    assert (tmp_path / "w.tar").stat().st_size == 100_000


def test_deleting_the_workspace_leaves_its_mount_point(env, tmp_path):
    env.exec("rm -rf /workspace")
    assert env.save_workspace(tmp_path / "w.tar", 10_000_000) is False
    with tarfile.open(tmp_path / "w.tar") as tar:
        assert [m.name for m in tar.getmembers()] == ["."]


def test_save_workspace_of_a_missing_folder_is_oserror(env, tmp_path):
    env.workdir = "/no/such/dir"
    try:
        with pytest.raises(OSError, match="tar failed"):
            env.save_workspace(tmp_path / "w.tar", 1000)
    finally:
        env.workdir = "/workspace"


def _volumes() -> str:
    return subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"label={docker_module.LABEL}"],
                          capture_output=True, text=True).stdout


def test_workspace_is_a_volume_that_keeps_the_image_files_and_is_removed_on_stop():
    tag = "harness-test-workspace"
    dockerfile = b"FROM alpine:latest\nRUN mkdir /workspace && echo baked > /workspace/f.txt\n"
    subprocess.run(["docker", "build", "-t", tag, "-"], input=dockerfile, capture_output=True, check=True)
    try:
        with DockerEnvironment(tag) as env:
            volume = env.volume
            assert volume in _volumes()
            assert env.read_file("f.txt") == b"baked\n"
        assert volume not in _volumes()
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


def test_another_workdir_keeps_the_image_files_there_and_is_the_grader_and_workspace_tar_root(tmp_path):
    tag = "harness-test-workdir"
    dockerfile = b"FROM alpine:latest\nRUN mkdir -p /app/sub && echo from-image > /app/sub/f.txt\nWORKDIR /app\n"
    subprocess.run(["docker", "build", "-t", tag, "-"], input=dockerfile, capture_output=True, check=True)
    try:
        with DockerEnvironment(tag, workdir="/app") as env:
            mounts = json.loads(subprocess.run(["docker", "inspect", "-f", "{{json .Mounts}}", env.container_id],
                                               capture_output=True, text=True, check=True).stdout)
            assert [(m["Name"], m["Destination"]) for m in mounts] == [(env.volume, "/app")]
            assert env.exec("pwd; cat sub/f.txt").stdout == "/app\nfrom-image\n"
            env.exec("echo by-agent > new.txt")
            with env.attach_grader(tag) as grader:
                assert grader.exec("pwd; cat new.txt sub/f.txt").stdout == "/app\nby-agent\nfrom-image\n"
            env.save_workspace(tmp_path / "w.tar", 10**6)
        with tarfile.open(tmp_path / "w.tar") as tar:
            assert {"./new.txt", "./sub/f.txt"} <= set(tar.getnames())
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


def test_attached_grader_sees_the_workspace_a_live_service_and_the_processes():
    with DockerEnvironment(IMAGE) as env:
        env.write_file("answer.txt", "42")
        env.exec("mkdir /trial-only && (while true; do echo up | nc -l -p 8080; done) > /dev/null 2>&1 &")
        with env.attach_grader(IMAGE) as grader:
            assert grader.read_file("/workspace/answer.txt") == b"42"
            assert grader.exec("nc -w 2 localhost 8080 < /dev/null").stdout == "up\n"
            assert "nc -l -p 8080" in grader.exec("ps").stdout
            grader.write_file("/workspace/from-grader.txt", "x")
            assert grader.exec("test -e /trial-only").exit_code != 0
        assert env.read_file("from-grader.txt") == b"x"
        assert env.state().running


def test_attached_grader_has_the_trial_limits():
    with DockerEnvironment(IMAGE, limits=Limits(memory="256m", cpus=0.5, pids_limit=64)) as env:
        with env.attach_grader(IMAGE) as grader:
            host = json.loads(subprocess.run(["docker", "inspect", "-f", "{{json .HostConfig}}", grader.container_id],
                                             capture_output=True, check=True).stdout)
    assert (host["Memory"], host["NanoCpus"], host["PidsLimit"]) == (256 * 2**20, 500_000_000, 64)


def test_stopping_the_trial_removes_its_grader_and_volume():
    env = DockerEnvironment(IMAGE)
    env.start()
    grader = env.attach_grader(IMAGE)
    grader.start()
    ids, volume = (env.container_id, grader.container_id), env.volume
    env.stop()
    assert all(_listed(i) == "" for i in ids) and volume not in _volumes()


def test_grader_that_fails_to_start_is_environment_lost():
    with DockerEnvironment(IMAGE) as env:
        with pytest.raises(EnvironmentLost):
            env.attach_grader("harness-no-such-image:nope").start()


def test_exec_env_reaches_the_command_but_never_the_docker_command_line(env, monkeypatch):
    seen = []
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        seen.append(argv)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr("agentic_harness.environments.docker.subprocess.Popen", popen)
    r = env.exec(["sh", "-c", 'echo "$HARNESS_TEST_SECRET"'], env={"HARNESS_TEST_SECRET": "s3cret-value"})
    assert r.stdout == "s3cret-value\n"
    assert "HARNESS_TEST_SECRET" in seen[0] and not any("s3cret-value" in a for a in seen[0])
    assert env.exec(["sh", "-c", 'echo "x$HARNESS_TEST_SECRET"']).stdout == "x\n"


def test_exec_as_another_user(env):
    assert env.exec(["id", "-un"], user="nobody").stdout == "nobody\n"
    assert env.exec(["id", "-un"]).stdout == "root\n"


def test_a_volume_still_in_use_is_retried_then_reported(monkeypatch, caplog):
    monkeypatch.setattr(docker_module, "VOLUME_RM_DELAY_S", 0)
    env = DockerEnvironment(IMAGE)
    env.start()
    volume = env.volume
    user = subprocess.run(["docker", "run", "-d", "--label", docker_module.LABEL, "-v", f"{volume}:/x", IMAGE, "sleep", "60"],
                          capture_output=True, text=True, check=True).stdout.strip()
    try:
        env.stop()
        assert f"leftover volume {volume}" in caplog.text and volume in _volumes()
    finally:
        subprocess.run(["docker", "rm", "-f", user], capture_output=True)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
