import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Self

from agentic_harness.types import Limits

DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False


class EnvironmentLost(RuntimeError):
    """The world itself is gone (verified, never agent-caused) -> infra_error, retried."""


class EndReason(Enum):
    EXPIRED = "expired"
    RESOURCE_LIMIT = "resource_limit"
    KILLED = "killed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EnvironmentState:
    running: bool
    end_reason: EndReason | None = None


@dataclass(frozen=True)
class Sidecar:
    image: str
    command: tuple[str, ...] = ()
    port: int | None = None
    # False for services the agent uses: shared PIDs would expose them
    shares_world: bool = True


class Environment(ABC):
    def __init__(self, image: str, workdir: str = "/workspace") -> None:
        self.image = image
        self.workdir = workdir

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def exec(self, cmd: str | list[str], timeout: float | None = DEFAULT_TIMEOUT, cwd: str | None = None,
             env: dict[str, str] | None = None, user: str | None = None) -> ExecResult:
        """Run a command: a string via `sh -c`, a list directly; timeout None = no limit."""

    def start_process(self, argv: list[str], env: dict[str, str] | None = None,
                      user: str | None = None) -> subprocess.Popen:
        """Start a long-running process in the world with its stdio piped to the host."""
        raise NotImplementedError(f"{type(self).__name__} can't run a process the harness talks to")

    @abstractmethod
    def state(self) -> EnvironmentState | None:
        """None if the world is gone or the backend is unreachable."""

    @abstractmethod
    def kill_agent_processes(self) -> None:
        """Kill everything the agent left running (before grading), while the world itself stays up."""

    @abstractmethod
    def save_workspace(self, local: Path, max_bytes: int) -> bool:
        """Write the workdir to `local` as a tar, never unpacked on the host; True if cut short."""

    @abstractmethod
    def attach(self, sidecar: Sidecar) -> "Environment":
        """A second world attached to this one, not started yet; stopped with this one."""

    def attach_grader(self, image: str) -> "Environment":
        """A grader: sees this world's workspace, network and processes."""
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


EnvironmentFactory = Callable[[str, int, Limits, str], Environment]
