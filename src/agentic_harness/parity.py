"""`harness parity`: does our harness score an agent the same as the benchmark's original workflow? Harbor's method:
N runs per side, mean ± SEM of each run's accuracy, and the two sides' run-score ranges must overlap (if they don't,
it is an adaptation error until shown otherwise). Per-task pass rates show where the sides differ."""

import statistics
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentic_harness.benchmarks.base import Benchmark
from agentic_harness.types import Task

# One side's runs: run -> task id -> score as a number, or None when that trial is left out (not graded, or crashed)
Scores = list[dict[str, float | None]]


def original_side(benchmark: Benchmark, tasks: list[Task], agent_name: str, model: str, runs: int, out_dir: Path,
                  concurrency: int) -> tuple[Scores, float, list[str]]:
    """Every task `runs` times through benchmark.original_workflow: the scores, the total spend, and the errors."""
    jobs = [(i, t) for i in range(runs) for t in tasks]
    errors: list[str] = []

    def one(job: tuple[int, Task]) -> tuple[float | None, float]:
        i, task = job
        try:
            result = benchmark.original_workflow(task, agent_name, model, out_dir / f"run-{i + 1}" / task.id)
        except Exception:  # infrastructure, like the harness's infra_error: that trial is left out, and listed
            errors.append(f"run {i + 1} {task.id}: {traceback.format_exc().strip().splitlines()[-1]}")
            return None, 0.0
        return benchmark.score_to_float(result.score), result.cost_usd

    with ThreadPoolExecutor(concurrency) as pool:
        results = list(pool.map(one, jobs))
    scores: Scores = [{} for _ in range(runs)]
    for (i, task), (score, _) in zip(jobs, results):
        scores[i][task.id] = score
    return scores, sum(cost for _, cost in results), sorted(errors)  # threads finish in any order


def summarize(scores: Scores) -> dict:
    """Each run's accuracy (mean over its scored trials), their mean ± sample SEM, and their range."""
    accuracies = [statistics.mean(s) for run in scores if (s := [v for v in run.values() if v is not None])]
    sem = statistics.stdev(accuracies) / len(accuracies) ** 0.5 if len(accuracies) > 1 else None
    return {
        "run_accuracies": accuracies,
        "mean": statistics.mean(accuracies) if accuracies else None,
        "sem": sem,
        "range": [min(accuracies), max(accuracies)] if accuracies else None,
    }


def pass_rates(scores: Scores) -> dict[str, float | None]:
    """Per task: the mean score over the runs that scored it."""
    task_ids = list(dict.fromkeys(t for run in scores for t in run))
    rates = {}
    for t in task_ids:
        values = [run[t] for run in scores if run.get(t) is not None]
        rates[t] = statistics.mean(values) if values else None
    return rates


def overlap(a: list[float] | None, b: list[float] | None) -> bool | None:
    """Do two [min, max] ranges overlap? None if either side has no scored run."""
    if not a or not b:
        return None
    return max(a[0], b[0]) <= min(a[1], b[1])
