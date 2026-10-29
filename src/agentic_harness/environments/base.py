"""The world a task runs in: somewhere to run commands and move files."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 60.0  # seconds per exec when the caller doesn't say; stops one hung command eating the whole budget


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


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
        """Run a command. A string runs through a shell (`sh -c`, for agents); a list runs the program directly (for graders/harness).

        `timeout` is in seconds; None means no timeout.
        """

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

    def __enter__(self) -> "Environment":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
