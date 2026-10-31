import sqlite3
import time
from statistics import mean


def run_metrics(run: sqlite3.Row, trials: list[sqlite3.Row], attempts: list[sqlite3.Row]) -> dict:
    started = [t for t in trials if t["attempt_count"] > 0]
    passed = sum(t["outcome"] == "graded" and t["score_float"] is not None and t["score_float"] >= 1 for t in trials)
    cost = sum(a["cost_usd"] or 0 for a in attempts)
    first = min((a["started_at"] for a in attempts if a["started_at"]), default=None)
    last = max((a["finished_at"] for a in attempts if a["finished_at"]), default=None)
    wall = None if first is None else (last if run["finished_at"] and last else time.time()) - first
    finished = sum(t["state"] == "done" and t["attempt_count"] > 0 for t in trials)
    return {
        "cost_usd": cost,
        "max_cost_usd": run["max_cost_usd"],
        "cost_per_task_usd": cost / len(started) if started else None,
        "cost_per_passing_task_usd": cost / passed if passed else None,
        "passed": passed,
        "input_tokens": sum(a["input_tokens"] or 0 for a in attempts),
        "output_tokens": sum(a["output_tokens"] or 0 for a in attempts),
        "model_calls": sum(a["model_calls"] or 0 for a in attempts),
        "avg_env_start_s": _mean_span(attempts, "started_at", "env_ready_at"),
        "avg_agent_s": _mean_span(attempts, "agent_start_at", "agent_end_at"),
        "avg_grading_s": _mean_span(attempts, "grade_start_at", "grade_end_at"),
        "total_queue_wait_s": sum(a["started_at"] - a["queued_at"] for a in attempts if a["started_at"] and a["queued_at"]),
        "setup_s": run["setup_finished_at"] - run["setup_started_at"] if run["setup_finished_at"] else None,
        "wall_s": wall,
        "trials_per_min": finished / (wall / 60) if wall else None,
    }


def _mean_span(attempts: list[sqlite3.Row], start: str, end: str) -> float | None:
    spans = [a[end] - a[start] for a in attempts if a[start] is not None and a[end] is not None]
    return mean(spans) if spans else None
