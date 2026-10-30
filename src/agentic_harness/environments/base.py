"""The world a task runs in: somewhere to run commands and move files."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Self

from agentic_harness.types import Limits

DEFAULT_TIMEOUT = 60.0  # seconds per exec when the caller doesn't say; stops one hung command eating the whole budget


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False  # stdout or stderr was cut to the environment's output cap


class EnvironmentLost(RuntimeError):
    """The world itself is gone or unreachable (not a command failing inside it) -> infra_error, retried.
    Each backend raises it (or a subclass, e.g. DockerError) only when that is verified, never for anything the
    agent can cause."""


class EndReason(Enum):
    """Why a world ended, in the runner's terms. Each backend translates its own evidence (Docker: exit code, OOM)."""

    EXPIRED = "expired"  # its lifetime ran out: ours to size -> infra_error, retried
    RESOURCE_LIMIT = "resource_limit"  # it exceeded limits WE set (memory, pids, ...) -> graded fail
    KILLED = "killed"  # something inside ended it -> graded fail
    UNKNOWN = "unknown"  # the backend can't tell -> infra_error


@dataclass(frozen=True)
class EnvironmentState:
    """Whether the world is still up, and if not, why it ended."""

    running: bool
    end_reason: EndReason | None = None  # set once it has ended


@dataclass(frozen=True)
class Sidecar:
    """A second world attached to a trial's world. It always shares the trial's network, so `localhost` is the same
    in both (a service one listens on, the other reaches)."""

    image: str
    command: tuple[str, ...] = ()  # its long-running process, e.g. a server; empty: none (it is exec'd into)
    port: int | None = None  # with a command: start() returns once something listens here
    # Also the trial's workspace and processes (a grader inspecting the world). False for a service the agent uses:
    # sharing processes would let the agent reach into it (e.g. through /proc/<pid>/root).
    shares_world: bool = True


class Environment(ABC):
    """One isolated world (e.g. one Docker container). Use as a context manager so it is always stopped."""

    def __init__(self, image: str) -> None:
        self.image = image

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def exec(self, cmd: str | list[str], timeout: float | None = DEFAULT_TIMEOUT, cwd: str | None = None) -> ExecResult:
        """Run a command: a string through `sh -c` (agents), a list directly (graders, the harness).
        `timeout` is in seconds; None means no timeout."""

    @abstractmethod
    def state(self) -> EnvironmentState | None:
        """None if the world is gone or the backend is unreachable."""

    @abstractmethod
    def kill_agent_processes(self) -> None:
        """Kill everything the agent left running (before grading), while the world itself stays up."""

    @abstractmethod
    def save_workspace(self, local: Path, max_bytes: int) -> bool:
        """Write the working directory to `local` as a tar file, never unpacked on the host (a symlink in it can't
        reach a host path). Stops at `max_bytes`; True if cut short. OSError if it can't be read."""

    @abstractmethod
    def attach(self, sidecar: Sidecar) -> "Environment":
        """A second world attached to this one, with the same limits. Not started yet: use it as a context manager
        (or start() it). It is stopped with this world at the latest."""

    def attach_grader(self, image: str) -> "Environment":
        """A grader: sees this world's workspace, network (e.g. a service on localhost) and processes."""
        return self.attach(Sidecar(image))

    @abstractmethod
    def write_file(self, path: str, content: str | bytes) -> None:
        """Create or overwrite one small file in the world from data in memory."""

    @abstractmethod
    def read_file(self, path: str) -> bytes: ...

    @abstractmethod
    def upload(self, local: Path, remote: str) -> None:
        """Copy a file or folder from the host into the world."""

    @abstractmethod
    def download(self, remote: str, local: Path) -> None:
        """Copy a file or folder from the world to the host."""

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


# How the runner gets a world for a trial, without knowing the backend: (image, lifetime_s, limits) -> Environment.
# DockerEnvironment's constructor is one.
EnvironmentFactory = Callable[[str, int, Limits], Environment]
