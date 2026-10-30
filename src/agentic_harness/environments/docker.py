"""An Environment backed by one Docker container, driven through the `docker` CLI (no SDK dependency)."""

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

START_TIMEOUT = 600.0  # seconds; generous because the first run may pull the image
DOCKER_TIMEOUT = 120.0  # seconds; default for every other docker call, so a stuck daemon can't hang a worker
CP_TIMEOUT = 600.0  # seconds; copies can be large
BUILD_TIMEOUT = 1800.0  # seconds; builds may pull base images and run setup scripts
PULL_TIMEOUT = 600.0  # seconds for one `docker pull`
MAX_OUTPUT = 1_000_000  # bytes kept per stream of one exec; the rest is read and thrown away
LABEL = "agentic-harness=1"  # on every container we start, so leftovers are easy to find
VOLUME_RM_TRIES = 4  # removing a workspace volume; waits VOLUME_RM_DELAY_S x try number between tries
VOLUME_RM_DELAY_S = 0.5
KILL_PASSES = 5  # kill_agent_processes: passes over /proc at most, so a fork loop can't keep it busy forever
SERVICE_START_TIMEOUT = 60.0  # seconds for an attached service to listen on its port

log = logging.getLogger("harness.docker")


class DockerError(EnvironmentLost):
    """The docker CLI itself failed (not a command inside the container): the container is gone or Docker is down."""


class DockerEnvironment(Environment):
    """One long-lived container. The workspace is a Docker volume of its own (not a host folder), so an attached
    grader can share it; use download() or save_workspace() to get files out. Attached sidecars join its network
    namespace (`--network container:`), so localhost is the same in all of them."""

    def __init__(self, image: str, lifetime_s: int = 7200, limits: Limits = Limits(), workdir: str = "/workspace") -> None:
        super().__init__(image)
        self.limits = limits  # memory, cpus, pids_limit become docker run flags
        self.lifetime_s = lifetime_s  # the container's main process is `sleep lifetime_s`: a self-destruct timer if the harness dies
        self.workdir = workdir  # default cwd for exec; relative paths resolve here
        self.container_id: str | None = None
        self.volume: str | None = None  # the workspace volume, removed in stop(); None for an attached grader
        self.sleep_pid: str | None = None  # the main `sleep` (child of the init, PID 1); kill_agent_processes spares it
        self.attached_to: str | None = None  # set by attach(): the trial container this one is attached to
        self.sidecar: Sidecar | None = None  # set by attach(): what it shares and runs
        self.attached: list[DockerEnvironment] = []  # sidecars attached to this one; stopped before it, or its volume stays in use

    def start(self) -> None:
        name = f"harness-{uuid.uuid4().hex[:8]}"
        try:
            if self.attached_to is None:
                # A named volume starts with the image's own /workspace files (Docker copies them in on first use)
                _docker("volume", "create", "--label", LABEL, name)
                self.volume = name
                world = ["-v", f"{name}:{self.workdir}"]
            else:
                t = self.attached_to
                world = ["--network", f"container:{t}"]  # localhost is the trial's
                if self.sidecar.shares_world:  # and the trial's workspace volume and processes
                    world += ["--volumes-from", t, "--pid", f"container:{t}"]
            # No --rm: a container that dies mid-trial must stay inspectable (state()); stop() removes it.
            # --init: sleep as PID 1 would never reap zombies.
            # --entrypoint: images with their own ENTRYPOINT would otherwise wrap our sleep.
            result = _docker(
                "run", "-d", "--init", "--label", LABEL, "--name", name, "-w", self.workdir, *world,
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
            # __exit__ won't run if start() fails; the name is known even if `run` never returned an id
            _rm(name)
            self.container_id = None
            self._remove_volume()
            raise

    def stop(self) -> None:
        """Remove attached sidecars, then the container and its workspace volume. Safe to call twice or before start()."""
        for sidecar in self.attached:
            sidecar.stop()
        if self.container_id is None:
            return
        _rm(self.container_id)
        self.container_id = None
        self.sleep_pid = None
        self._remove_volume()

    def attach(self, sidecar: Sidecar) -> "DockerEnvironment":
        env = DockerEnvironment(sidecar.image, self.lifetime_s, self.limits, self.workdir)
        env.attached_to = self._id()
        env.sidecar = sidecar
        self.attached.append(env)
        return env

    def _start_service(self) -> None:
        """Run the sidecar's command beside its keep-alive sleep (which stays the self-destruct timer), then wait until
        it listens on its port. Its output goes to PID 1's, so `docker logs` shows it."""
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
        """Whether a TCP socket listens on `port`, from the kernel's own table: no tool (nc, curl) needed in the image."""
        table = _decode(_docker("exec", self._id(), "cat", "/proc/net/tcp", "/proc/net/tcp6", check=False).stdout)
        # each row: sl local_address(hex ip:hex port) rem_address st(0A = LISTEN) ...
        return any(f[1].endswith(f":{port:04X}") and f[3] == "0A" for f in (line.split() for line in table.splitlines()[1:]) if len(f) > 3)

    def exec(self, cmd: str | list[str], timeout: float | None = DEFAULT_TIMEOUT, cwd: str | None = None,
             env: dict[str, str] | None = None, user: str | None = None) -> ExecResult:
        argv = ["sh", "-c", cmd] if isinstance(cmd, str) else cmd
        # `-e NAME` without a value: docker takes it from its own environment, so values never show in `ps` on the host
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
            # Killing the `docker exec` client does NOT kill the process inside the container;
            # kill_agent_processes() cleans up leftovers before grading.
            proc.kill()
            proc.wait()
            timed_out = True
        for reader in readers:
            reader.join()
        truncated = len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT
        stdout, stderr = _decode(out[:MAX_OUTPUT]), _decode(err[:MAX_OUTPUT])
        if timed_out:
            return ExecResult(-1, stdout, stderr, timed_out=True, truncated=truncated)
        # docker's own failures look like a command's (exit 1/126/127). Raise DockerError (infra_error, excluded)
        # ONLY when the container is verifiably gone: anything else the agent can cause (delete /workspace, print a
        # fake docker message), and it must not be able to turn a failing trial into an excluded one.
        message = stderr or stdout  # docker prints on stderr or, for OCI errors, stdout
        if proc.returncode != 0 and message.startswith(("Error response from daemon", "OCI runtime exec failed")):
            if not self._alive():
                raise DockerError("container is gone")
        return ExecResult(proc.returncode, stdout, stderr, truncated=truncated)

    def state(self) -> EnvironmentState | None:
        """From `docker inspect`'s State. None if Docker is unreachable or the container is gone (both infra_error)."""
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
        """Kill every process except PID 1 (the init) and its `sleep`. Not `kill -9 -1`: that kills the sleep too,
        and the init exits with it, taking the container down. Repeated until a pass kills nothing: /proc is listed
        once per pass, so a process forked after the listing (e.g. by a fork loop not yet killed) survives that pass."""
        script = (
            f'i=0; while [ $i -lt {KILL_PASSES} ]; do i=$((i+1)); n=0; '
            'for p in /proc/[0-9]*; do p=${p#/proc/}; [ "$p" = 1 ] || [ "$p" = "$1" ] || [ "$p" = $$ ] || '
            '{ kill -9 "$p" 2>/dev/null && n=$((n+1)); }; done; [ $n = 0 ] && break; done; true'
        )
        self.exec(["sh", "-c", script, "sh", self.sleep_pid])

    def save_workspace(self, local: Path, max_bytes: int) -> bool:
        """`tar` inside the container, streamed out through `docker exec` and written as is. Not download(): `docker cp`
        unpacks onto the host, where a symlink the agent planted would point at a host path."""
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
        truncated = written >= max_bytes and bool(proc.stdout.read1(1))  # at the cap with more still coming
        proc.kill()  # stops a cut-short tar; harmless if it already finished
        proc.wait()
        proc.stdout.close()
        reader.join()
        if not truncated and proc.returncode != 0:
            if not self._alive():
                raise DockerError("container is gone")
            raise OSError(f"tar failed: {_decode(err).strip()}")  # e.g. the agent deleted the workspace
        return truncated

    def write_file(self, path: str, content: str | bytes) -> None:
        data = content.encode() if isinstance(content, str) else content
        # Bytes go in via stdin; the path is passed as $1 so the shell never parses it
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
            raise OSError(message)  # e.g. a directory, or permission denied
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
        """Retried: a container that used it may still be going away. A volume that stays is logged, not raised
        (`harness cleanup` removes it later, by its label)."""
        if self.volume is None:
            return
        error = ""
        for attempt in range(VOLUME_RM_TRIES):
            if attempt:
                time.sleep(VOLUME_RM_DELAY_S * attempt)
            try:
                result = _docker("volume", "rm", "-f", self.volume, check=False)
            except DockerError as e:  # timed out
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
    """Build `tag` from Dockerfile text (sent on stdin) with `context` as the build folder."""
    _docker("build", "-t", tag, "-f", "-", str(context), input=dockerfile.encode(), timeout=BUILD_TIMEOUT)


def _end_reason(state: dict) -> EndReason:
    """Docker's evidence in the runner's terms. The main process is our `sleep`: it exits 0 only when its lifetime
    runs out; a signal (e.g. `kill 1`, forwarded by the init) makes the exit code non-zero."""
    if state["Status"] != "exited":  # e.g. "dead" (Docker failed to stop or remove it): not the agent's doing
        return EndReason.UNKNOWN
    if state["OOMKilled"]:  # over our --memory limit
        return EndReason.RESOURCE_LIMIT
    return EndReason.EXPIRED if state["ExitCode"] == 0 else EndReason.KILLED


def image_id(image: str) -> str | None:
    """The local image's content ID (changes whenever it is rebuilt), or None if it isn't local."""
    result = _docker("image", "inspect", "-f", "{{.Id}}", image, check=False)
    return _decode(result.stdout).strip() if result.returncode == 0 else None


def ensure_image(image: str, timeout: float = PULL_TIMEOUT) -> None:
    """Pull `image` unless it is already local, so later trials need no network to start it (e.g. a grader image).
    Bounded: a first pull can hang silently (e.g. in a sandboxed shell), and setup must fail, not wait forever."""
    if _docker("image", "inspect", image, check=False).returncode != 0:
        try:
            _docker("pull", image, timeout=timeout)
        except DockerError as e:
            raise DockerError(f"{e} while pulling {image} (try `docker pull {image}` by hand)") from None


def _docker(
    *args: str, input: bytes | None = None, timeout: float | None = DOCKER_TIMEOUT, check: bool = True
) -> subprocess.CompletedProcess:
    """Run one `docker` command; raise DockerError if it times out, or (with check) if it fails."""
    try:
        result = subprocess.run(["docker", *args], input=input, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DockerError(f"docker {args[0]} timed out after {timeout}s") from None
    if check and result.returncode != 0:
        raise DockerError(f"docker {args[0]} failed: {_decode(result.stderr).strip()}")
    return result


def _rm(container: str) -> None:
    """Best effort: errors and timeouts are ignored."""
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
