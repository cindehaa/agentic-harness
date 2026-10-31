"""Secret values become *** before anything is written to disk or the database: those in `.env`, any secret-named
variable in the harness's own environment, and every variable an agent forwards into its worlds."""

import functools
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from agentic_harness.paths import REPO_ROOT

SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")  # an .env name containing one of these holds a secret
MIN_LENGTH = 8  # shorter values (e.g. "true") would also replace ordinary words


def secret_values(env_file: Path) -> tuple[str, ...]:
    """The secret values in an .env file, longest first (so a secret that contains another is replaced whole)."""
    if not env_file.exists():
        return ()
    values = []
    for line in env_file.read_text().splitlines():
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        value = value.strip().strip("'\"")
        if sep and not name.startswith("#") and _secret_name(name) and len(value) >= MIN_LENGTH:
            values.append(value)
    return tuple(sorted(values, key=len, reverse=True))


def environ_secrets(environ: Mapping[str, str]) -> set[str]:
    """The values of secret-named variables (same rule as .env), e.g. a key exported in the shell, not in .env."""
    return {value for name, value in environ.items() if _secret_name(name) and len(value) >= MIN_LENGTH}


_forwarded: set[str] = set()


def add_forwarded(names: Iterable[str]) -> None:
    """Variables the harness forwards into a world (an agent's forward_env): their values are redacted from now on,
    whatever their names, since the agent can print them."""
    _forwarded.update(value for name in names if len(value := os.environ.get(name, "")) >= MIN_LENGTH)


def _secret_name(name: str) -> bool:
    return any(w in name.upper() for w in SECRET_WORDS)


@functools.cache
def _env_file_secrets() -> tuple[str, ...]:
    return secret_values(REPO_ROOT / ".env")


def _secrets() -> tuple[str, ...]:
    """Longest first. The environment is read each time: it can change after start-up (the CLI loads .env)."""
    return tuple(sorted(set(_env_file_secrets()) | environ_secrets(os.environ) | _forwarded, key=len, reverse=True))


def redact(text: str, secrets: tuple[str, ...] | None = None) -> str:
    for secret in _secrets() if secrets is None else secrets:
        text = text.replace(secret, "***")
    return text


def redact_file(path: Path) -> None:
    """Redact a file in place, e.g. one the agent wrote itself (mini's trajectory). Binary files are left alone.
    A tar file keeps its size: each secret becomes as many `*` bytes, so the archive stays readable."""
    if path.suffix == ".tar":
        data = path.read_bytes()
        clean = data
        for secret in _secrets():
            clean = clean.replace(secret.encode(), b"*" * len(secret.encode()))
        if clean != data:
            path.write_bytes(clean)
        return
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        return
    if (clean := redact(text)) != text:
        path.write_text(clean)
