"""Redaction at the log boundary (`ideation/security.md`): secret values from `.env` become *** before anything is
written to disk or the database."""

import functools
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
        if sep and not name.startswith("#") and any(w in name.upper() for w in SECRET_WORDS) and len(value) >= MIN_LENGTH:
            values.append(value)
    return tuple(sorted(values, key=len, reverse=True))


@functools.cache
def _secrets() -> tuple[str, ...]:
    return secret_values(REPO_ROOT / ".env")  # read once per process


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
