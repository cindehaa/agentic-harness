"""Filesystem locations the harness needs."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # where benchmarks/ and agents/ live
RUNS_DIR = REPO_ROOT / "runs"  # every run's attempt folders, and the database
DB_PATH = RUNS_DIR / "harness.db"
