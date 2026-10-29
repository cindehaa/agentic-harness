"""An Environment backed by one Docker container, driven through the `docker` CLI (no SDK dependency)."""

import posixpath
import subprocess
import uuid
from pathlib import Path

from agentic_harness.environments.base import DEFAULT_TIMEOUT, Environment, ExecResult

START_TIMEOUT = 600.0  # seconds; generous because the first run may pull the image
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
        result = _docker(
            "run", "-d", "--label", LABEL, "--name", name, "-w", self.workdir,
            self.image, "sleep", str(self.lifetime_s),
            timeout=START_TIMEOUT,
        )
        self.container_id = result.stdout.decode().strip()
        try:
            _docker("exec", self.container_id, "mkdir", "-p", self.workdir)
        except DockerError:
            self.stop()  # __exit__ won't run if start() fails, so clean up here
            raise

    def stop(self) -> None:
        """Remove the container. Safe to call twice or before start()."""
        if self.container_id is None:
            return
        subprocess.run(["docker", "rm", "-f", self.container_id], capture_output=True)  # errors ignored: best effort
        self.container_id = None

    def exec(self, cmd: str | list[str], timeout: float | None = DEFAULT_TIMEOUT, cwd: str | None = None) -> ExecResult:
        argv = ["sh", "-c", cmd] if isinstance(cmd, str) else cmd
        full = ["docker", "exec", "-w", cwd or self.workdir, self._id(), *argv]
        try:
            result = subprocess.run(full, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            # Killing the `docker exec` client does NOT kill the process inside the container.
            # Leftover processes are killed before grading (`kill -9 -1`); Phase 6 may harden this.
            return ExecResult(-1, _decode(e.stdout), _decode(e.stderr), timed_out=True)
        return ExecResult(result.returncode, _decode(result.stdout), _decode(result.stderr))

    def write_file(self, path: str, content: str | bytes) -> None:
        data = content.encode() if isinstance(content, str) else content
        # Bytes go in via stdin; the path is passed as $1 so the shell never parses it
        script = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
        _docker("exec", "-i", self._id(), "sh", "-c", script, "sh", path, input=data)

    def read_file(self, path: str) -> bytes:
        result = subprocess.run(["docker", "exec", self._id(), "cat", path], capture_output=True)
        if result.returncode != 0:
            running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", self._id()], capture_output=True)
            if running.returncode != 0 or running.stdout.strip() != b"true":
                raise DockerError("container is gone")  # not the agent's fault, so not FileNotFoundError
            raise FileNotFoundError(path)
        return result.stdout

    def upload(self, local: Path, remote: str) -> None:
        """Copying a folder to a path that doesn't exist creates it as a copy of the folder."""
        remote = posixpath.join(self.workdir, remote)  # docker cp resolves relative paths from /, not the workdir
        _docker("exec", self._id(), "mkdir", "-p", posixpath.dirname(remote))
        _docker("cp", str(local), f"{self._id()}:{remote}")

    def download(self, remote: str, local: Path) -> None:
        remote = posixpath.join(self.workdir, remote)
        local.parent.mkdir(parents=True, exist_ok=True)
        _docker("cp", f"{self._id()}:{remote}", str(local))

    def _id(self) -> str:
        if self.container_id is None:
            raise DockerError("not started")
        return self.container_id


def _docker(*args: str, input: bytes | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run one `docker` command; raise DockerError if it fails."""
    result = subprocess.run(["docker", *args], input=input, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise DockerError(f"docker {args[0]} failed: {_decode(result.stderr).strip()}")
    return result


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")
