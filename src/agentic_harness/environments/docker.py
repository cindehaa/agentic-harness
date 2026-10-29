"""An Environment backed by one Docker container, driven through the `docker` CLI (no SDK dependency)."""

import posixpath
import subprocess
import threading
import uuid
from pathlib import Path
from typing import IO

from agentic_harness.environments.base import DEFAULT_TIMEOUT, Environment, ExecResult

START_TIMEOUT = 600.0  # seconds; generous because the first run may pull the image
DOCKER_TIMEOUT = 120.0  # seconds; default for every other docker call, so a stuck daemon can't hang a worker
CP_TIMEOUT = 600.0  # seconds; copies can be large
BUILD_TIMEOUT = 1800.0  # seconds; builds may pull base images and run setup scripts
MAX_OUTPUT = 1_000_000  # bytes kept per stream of one exec; the rest is read and thrown away
LABEL = "agentic-harness=1"  # on every container we start, so leftovers are easy to find


class DockerError(RuntimeError):
    """The docker CLI itself failed (not a command inside the container)."""


class DockerEnvironment(Environment):
    """One long-lived container. The workspace lives only inside it (no bind mount); use download() to get files out."""

    def __init__(self, image: str, lifetime_s: int = 7200, workdir: str = "/workspace") -> None:
        super().__init__(image)
        self.lifetime_s = lifetime_s  # the container's main process is `sleep lifetime_s`: a self-destruct timer if the harness dies
        self.workdir = workdir  # default cwd for exec; relative paths resolve here
        self.container_id: str | None = None

    def start(self) -> None:
        name = f"harness-{uuid.uuid4().hex[:8]}"
        try:
            # --rm: deleted when sleep expires. --init: sleep as PID 1 would never reap zombies.
            # --entrypoint: images with their own ENTRYPOINT would otherwise wrap our sleep.
            result = _docker(
                "run", "-d", "--rm", "--init", "--label", LABEL, "--name", name, "-w", self.workdir,
                "--entrypoint", "sleep", self.image, str(self.lifetime_s),
                timeout=START_TIMEOUT,
            )
            self.container_id = result.stdout.decode().strip()
            _docker("exec", self.container_id, "mkdir", "-p", self.workdir)
        except BaseException:
            # __exit__ won't run if start() fails; the name is known even if `run` never returned an id
            _rm(name)
            self.container_id = None
            raise

    def stop(self) -> None:
        """Remove the container. Safe to call twice or before start()."""
        if self.container_id is None:
            return
        _rm(self.container_id)
        self.container_id = None

    def exec(self, cmd: str | list[str], timeout: float | None = DEFAULT_TIMEOUT, cwd: str | None = None) -> ExecResult:
        argv = ["sh", "-c", cmd] if isinstance(cmd, str) else cmd
        full = ["docker", "exec", "-w", cwd or self.workdir, self._id(), *argv]
        proc = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = bytearray(), bytearray()
        readers = [_drain(proc.stdout, out), _drain(proc.stderr, err)]
        try:
            proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            # Killing the `docker exec` client does NOT kill the process inside the container.
            # Leftover processes are killed before grading (`kill -9 -1`); Phase 6 may harden this.
            proc.kill()
            proc.wait()
            timed_out = True
        for reader in readers:
            reader.join()
        truncated = len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT
        stdout, stderr = _decode(out[:MAX_OUTPUT]), _decode(err[:MAX_OUTPUT])
        if timed_out:
            return ExecResult(-1, stdout, stderr, timed_out=True, truncated=truncated)
        # docker's own failures look like a command's (exit 1/126/127). Raise DockerError (-> infra_error, excluded
        # and retried) ONLY when the container is verifiably gone. Anything else, like a missing cwd or an OCI exec
        # error, the agent can cause (delete /workspace, or just print a fake docker message), so it stays a normal
        # failed command; otherwise an agent could turn a failing trial into an excluded one.
        message = stderr or stdout  # docker prints on stderr or, for OCI errors, stdout
        if proc.returncode != 0 and message.startswith(("Error response from daemon", "OCI runtime exec failed")):
            if not self._alive():
                raise DockerError("container is gone")
        return ExecResult(proc.returncode, stdout, stderr, truncated=truncated)

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

    def _alive(self) -> bool:
        result = _docker("inspect", "-f", "{{.State.Running}}", self._id(), check=False)
        return result.returncode == 0 and result.stdout.strip() == b"true"


def build_image(dockerfile: str, context: Path, tag: str) -> None:
    """Build `tag` from Dockerfile text (sent on stdin) with `context` as the build folder."""
    _docker("build", "-t", tag, "-f", "-", str(context), input=dockerfile.encode(), timeout=BUILD_TIMEOUT)


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
