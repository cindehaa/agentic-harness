import json
import logging
import os
import posixpath
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import IO

from agentic_harness.environments.base import (
    DEFAULT_TIMEOUT, EndReason, Environment, EnvironmentLost, EnvironmentState, ExecResult, Sidecar,
)
from agentic_harness.types import Limits

START_TIMEOUT = 600.0
DOCKER_TIMEOUT = 120.0
CP_TIMEOUT = 600.0
BUILD_TIMEOUT = 1800.0
PULL_TIMEOUT = 600.0
MAX_OUTPUT = 1_000_000
LABEL = "agentic-harness=1"
ATTEMPT_LABEL = "agentic-harness.attempt"
IMAGE_PREFIX = "harness-"
VOLUME_RM_TRIES = 4
VOLUME_RM_DELAY_S = 0.5
KILL_PASSES = 5
SERVICE_START_TIMEOUT = 60.0

log = logging.getLogger("harness.docker")


class DockerError(EnvironmentLost):
    ...


class DockerEnvironment(Environment):
    def __init__(self, image: str, lifetime_s: int = 7200, limits: Limits = Limits(), workdir: str = "/workspace",
                 labels: dict[str, str] | None = None) -> None:
        super().__init__(image, workdir)
        self.labels = labels or {}
        self.limits = limits
        self.lifetime_s = lifetime_s
        self.container_id: str | None = None
        self.volume: str | None = None
        self.sleep_pid: str | None = None
        self.attached_to: str | None = None
        self.sidecar: Sidecar | None = None
        self.attached: list[DockerEnvironment] = []

    def start(self) -> None:
        name = f"harness-{uuid.uuid4().hex[:8]}"
        try:
            if self.attached_to is None:
                # a named volume starts with the image's own /workspace files
                _docker("volume", "create", *self._label_flags(), name)
                self.volume = name
                world = ["-v", f"{name}:{self.workdir}"]
            else:
                t = self.attached_to
                world = ["--network", f"container:{t}"]
                if self.sidecar.shares_world:
                    world += ["--volumes-from", t, "--pid", f"container:{t}"]
            # No --rm: a dead container must stay inspectable; stop() removes it.
            # --init: sleep as PID 1 would never reap zombies.
            # --entrypoint: images with their own ENTRYPOINT would otherwise wrap our sleep.
            result = _docker(
                "run", "-d", "--init", *self._label_flags(), "--name", name, "-w", self.workdir, *world,
                "--memory", self.limits.memory, "--cpus", str(self.limits.cpus), "--pids-limit", str(self.limits.pids_limit),
                "--entrypoint", "sleep", self.image, str(self.lifetime_s),
                timeout=START_TIMEOUT,
            )
            self.container_id = result.stdout.decode().strip()
            if self.attached_to is None:  # in an attached grader, PID 1 is the trial's init
                _docker("exec", self.container_id, "mkdir", "-p", self.workdir)
                self.sleep_pid = _decode(_docker("exec", self.container_id, "cat", "/proc/1/task/1/children").stdout).split()[0]
            elif self.sidecar.command:
                self._start_service()
        except BaseException:
            # __exit__ won't run if start() fails
            _rm(name)
            self.container_id = None
            self._remove_volume()
            raise

    def stop(self) -> None:
        for sidecar in self.attached:
            sidecar.stop()
        if self.container_id is None:
            return
        _rm(self.container_id)
        self.container_id = None
        self.sleep_pid = None
        self._remove_volume()

    def _label_flags(self) -> list[str]:
        return ["--label", LABEL, *(a for k, v in self.labels.items() for a in ("--label", f"{k}={v}"))]

    def attach(self, sidecar: Sidecar) -> "DockerEnvironment":
        env = DockerEnvironment(sidecar.image, self.lifetime_s, self.limits, self.workdir, self.labels)
        env.attached_to = self._id()
        env.sidecar = sidecar
        self.attached.append(env)
        return env

    def _start_service(self) -> None:
        _docker("exec", "-d", self._id(), "sh", "-c", 'exec "$@" > /proc/1/fd/1 2> /proc/1/fd/2', "sh", *self.sidecar.command)
        if self.sidecar.port is None:
            return
        deadline = time.monotonic() + SERVICE_START_TIMEOUT
        while not self._listening(self.sidecar.port):
            if time.monotonic() > deadline:
                logs = _docker("logs", "--tail", "20", self._id(), check=False)
                raise DockerError(f"{self.image} not listening on port {self.sidecar.port} after {SERVICE_START_TIMEOUT:.0f}s: "
                                  f"{_decode(logs.stdout + logs.stderr).strip()}")
            time.sleep(0.2)

    def _listening(self, port: int) -> bool:
        table = _decode(_docker("exec", self._id(), "cat", "/proc/net/tcp", "/proc/net/tcp6", check=False).stdout)
        return any(f[1].endswith(f":{port:04X}") and f[3] == "0A" for f in (line.split() for line in table.splitlines()[1:]) if len(f) > 3)

    def exec(self, cmd: str | list[str], timeout: float | None = DEFAULT_TIMEOUT, cwd: str | None = None,
             env: dict[str, str] | None = None, user: str | None = None) -> ExecResult:
        argv = ["sh", "-c", cmd] if isinstance(cmd, str) else cmd
        # `-e NAME` alone: the value comes from docker's env, never shown in `ps`
        names = [a for name in env or {} for a in ("-e", name)]
        full = ["docker", "exec", "-w", cwd or self.workdir, *names, *(["-u", user] if user else []), self._id(), *argv]
        proc = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=os.environ | env if env else None)
        out, err = bytearray(), bytearray()
        readers = [_drain(proc.stdout, out), _drain(proc.stderr, err)]
        try:
            proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            # killing `docker exec` leaves the process running inside the container
            proc.kill()
            proc.wait()
            timed_out = True
        for reader in readers:
            reader.join()
        truncated = len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT
        stdout, stderr = _decode(out[:MAX_OUTPUT]), _decode(err[:MAX_OUTPUT])
        if timed_out:
            return ExecResult(-1, stdout, stderr, timed_out=True, truncated=truncated)
        # raise only if the container is gone: an agent can fake docker's errors
        message = stderr or stdout  # docker prints on stderr or, for OCI errors, stdout
        if proc.returncode != 0 and message.startswith(("Error response from daemon", "OCI runtime exec failed")):
            if not self._alive():
                raise DockerError("container is gone")
        return ExecResult(proc.returncode, stdout, stderr, truncated=truncated)

    def start_process(self, argv: list[str], env: dict[str, str] | None = None,
                      user: str | None = None) -> subprocess.Popen:
        names = [a for name in env or {} for a in ("-e", name)]
        full = ["docker", "exec", "-i", "-w", self.workdir, *names, *(["-u", user] if user else []), self._id(), *argv]
        return subprocess.Popen(full, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=os.environ | env if env else None)

    def state(self) -> EnvironmentState | None:
        try:
            result = _docker("inspect", "-f", "{{json .State}}", self._id(), check=False)
        except DockerError:
            return None
        if result.returncode != 0:
            return None
        state = json.loads(result.stdout)
        if state["Running"]:
            return EnvironmentState(running=True)
        return EnvironmentState(running=False, end_reason=_end_reason(state))

    def kill_agent_processes(self) -> None:
        """Kill all but PID 1 and its sleep (`kill -9 -1` takes the container down)."""
        script = (
            f'i=0; while [ $i -lt {KILL_PASSES} ]; do i=$((i+1)); n=0; '
            'for p in /proc/[0-9]*; do p=${p#/proc/}; [ "$p" = 1 ] || [ "$p" = "$1" ] || [ "$p" = $$ ] || '
            '{ kill -9 "$p" 2>/dev/null && n=$((n+1)); }; done; [ $n = 0 ] && break; done; true'
        )
        self.exec(["sh", "-c", script, "sh", self.sleep_pid])

    def save_workspace(self, local: Path, max_bytes: int) -> bool:
        """Not download(): `docker cp` unpacks on the host, where a planted symlink escapes."""
        argv = ["docker", "exec", self._id(), "tar", "-cf", "-", "-C", self.workdir, "."]
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        err = bytearray()
        reader = _drain(proc.stderr, err)  # read alongside stdout, so many tar warnings can't block it
        written = 0
        with local.open("wb") as f:
            while written < max_bytes and (chunk := proc.stdout.read1(65536)):
                chunk = chunk[: max_bytes - written]
                f.write(chunk)
                written += len(chunk)
        truncated = written >= max_bytes and bool(proc.stdout.read1(1))
        proc.kill()  # stops a cut-short tar; harmless if it already finished
        proc.wait()
        proc.stdout.close()
        reader.join()
        if not truncated and proc.returncode != 0:
            if not self._alive():
                raise DockerError("container is gone")
            raise OSError(f"tar failed: {_decode(err).strip()}")
        return truncated

    def write_file(self, path: str, content: str | bytes) -> None:
        data = content.encode() if isinstance(content, str) else content
        # the path is passed as $1 so the shell never parses it
        script = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
        _docker("exec", "-i", self._id(), "sh", "-c", script, "sh", path, input=data)

    def read_file(self, path: str) -> bytes:
        result = _docker("exec", self._id(), "cat", path, check=False)
        if result.returncode != 0:
            if not self._alive():
                raise DockerError("container is gone")  # not the agent's fault, so not FileNotFoundError
            message = _decode(result.stderr).strip()
            if "No such file" in message:
                raise FileNotFoundError(path)
            raise OSError(message)
        return result.stdout

    def upload(self, local: Path, remote: str) -> None:
        """Replace `remote` with a copy of `local` (never nests into an existing folder the agent may have planted)."""
        remote = posixpath.join(self.workdir, remote)  # docker cp resolves relative paths from /, not the workdir
        script = 'rm -rf "$1" && mkdir -p "$(dirname "$1")"' + (' && mkdir -p "$1"' if local.is_dir() else "")
        _docker("exec", self._id(), "sh", "-c", script, "sh", remote)
        source = f"{local}/." if local.is_dir() else str(local)  # `/.` copies the folder's contents, not the folder
        _docker("cp", source, f"{self._id()}:{remote}", timeout=CP_TIMEOUT)

    def download(self, remote: str, local: Path) -> None:
        """Copy to `local`, which must not exist yet (host paths are never deleted or merged into)."""
        remote = posixpath.join(self.workdir, remote)
        if local.exists() or local.is_symlink():
            raise FileExistsError(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        _docker("cp", f"{self._id()}:{remote}", str(local), timeout=CP_TIMEOUT)

    def _id(self) -> str:
        if self.container_id is None:
            raise DockerError("not started")
        return self.container_id

    def _remove_volume(self) -> None:
        if self.volume is None:
            return
        error = ""
        for attempt in range(VOLUME_RM_TRIES):
            if attempt:
                time.sleep(VOLUME_RM_DELAY_S * attempt)
            try:
                result = _docker("volume", "rm", "-f", self.volume, check=False)
            except DockerError as e:
                error = str(e)
                continue
            if result.returncode == 0:
                break
            error = _decode(result.stderr).strip()
        else:
            log.warning("leftover volume %s (harness cleanup removes it): %s", self.volume, error)
        self.volume = None

    def _alive(self) -> bool:
        result = _docker("inspect", "-f", "{{.State.Running}}", self._id(), check=False)
        return result.returncode == 0 and result.stdout.strip() == b"true"


def build_image(dockerfile: str, context: Path, tag: str) -> None:
    _docker("build", "-t", tag, "-f", "-", str(context), input=dockerfile.encode(), timeout=BUILD_TIMEOUT)


def _end_reason(state: dict) -> EndReason:
    """The sleep exits 0 only when its lifetime runs out; a signal makes it non-zero."""
    if state["Status"] != "exited":  # e.g. "dead": not the agent's doing
        return EndReason.UNKNOWN
    if state["OOMKilled"]:
        return EndReason.RESOURCE_LIMIT
    return EndReason.EXPIRED if state["ExitCode"] == 0 else EndReason.KILLED


def image_id(image: str) -> str | None:
    result = _docker("image", "inspect", "-f", "{{.Id}}", image, check=False)
    return _decode(result.stdout).strip() if result.returncode == 0 else None


def ensure_image(image: str, timeout: float = PULL_TIMEOUT) -> None:
    if _docker("image", "inspect", image, check=False).returncode != 0:
        try:
            _docker("pull", image, timeout=timeout)
        except DockerError as e:
            raise DockerError(f"{e} while pulling {image} (try `docker pull {image}` by hand)") from None


def remove_leftovers(keep_attempts: set[str]) -> tuple[list[str], list[str], list[str]]:
    removed: tuple[list[str], list[str]] = ([], [])
    failed = []
    for i, kind in enumerate(("container", "volume")):
        listed = _docker(kind, "ls", *(["-a"] if kind == "container" else []), "--filter", f"label={LABEL}",
                         "--format", f'{{{{.Name{"s" if kind == "container" else ""}}}}}\t{{{{.Label "{ATTEMPT_LABEL}"}}}}')
        for line in _decode(listed.stdout).splitlines():
            name, _, attempt = line.partition("\t")
            if attempt and attempt in keep_attempts:
                continue
            result = _docker(kind, "rm", "-f", name, check=False)
            if result.returncode == 0:
                removed[i].append(name)
            else:
                failed.append(f"{kind} {name}: {_decode(result.stderr).strip()}")
    return removed[0], removed[1], failed


def remove_images(keep: set[str]) -> tuple[list[str], int, list[str]]:
    listed = _decode(_docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}").stdout).splitlines()
    images = [line.split("\t") for line in listed if line.startswith(IMAGE_PREFIX) and not line.endswith(":<none>")]
    sizes = {}
    for _, image_id in images:
        size = _docker("image", "inspect", "-f", "{{.Size}}", image_id, check=False)
        sizes[image_id] = int(_decode(size.stdout).strip() or 0)
    removed, failed = [], []
    for tag, _ in images:
        if tag in keep:
            continue
        result = _docker("image", "rm", tag, check=False)
        (removed if result.returncode == 0 else failed).append(tag)
    freed = sum(size for image_id, size in sizes.items()
                if _docker("image", "inspect", image_id, check=False).returncode != 0)
    return removed, freed, failed


def _docker(
    *args: str, input: bytes | None = None, timeout: float | None = DOCKER_TIMEOUT, check: bool = True
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["docker", *args], input=input, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DockerError(f"docker {args[0]} timed out after {timeout}s") from None
    if check and result.returncode != 0:
        raise DockerError(f"docker {args[0]} failed: {_decode(result.stderr).strip()}")
    return result


def _rm(container: str) -> None:
    try:
        _docker("rm", "-f", container, check=False)
    except DockerError:
        pass


def _drain(stream: IO[bytes], buf: bytearray) -> threading.Thread:
    """Read `stream` to EOF in a thread, keeping MAX_OUTPUT + 1 bytes (the +1 marks truncation)."""

    def run() -> None:
        while chunk := stream.read1(65536):
            buf.extend(chunk[: MAX_OUTPUT + 1 - len(buf)])
        stream.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")
