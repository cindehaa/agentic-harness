"""Tamper detection: fingerprint the files a benchmark says matter for grading, so changes can be flagged (never scored)."""

import hashlib

from agentic_harness.environments import Environment


def snapshot(env: Environment, paths: list[str]) -> dict[str, str | None]:
    """sha256 of each file's bytes; None if the file is missing."""
    hashes: dict[str, str | None] = {}
    for path in paths:
        try:
            hashes[path] = hashlib.sha256(env.read_file(path)).hexdigest()
        except FileNotFoundError:
            hashes[path] = None
    return hashes


def changed(before: dict[str, str | None], after: dict[str, str | None]) -> list[str]:
    """Paths whose hash differs between two snapshots, including files deleted or created."""
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
