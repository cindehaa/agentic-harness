"""Filesystem locations the harness needs."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # src/agentic_harness/paths.py -> repo root (where benchmarks/ lives)
