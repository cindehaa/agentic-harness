"""CLI for the agentic harness system."""

import json
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import click

from agentic_harness.agents import resolve_agent
from agentic_harness.benchmarks import BENCHMARKS
from agentic_harness.paths import REPO_ROOT
from agentic_harness.runner import run_trial
from agentic_harness.types import Limits, Outcome, Task


@click.group()
def cli():
    """Agentic Harness"""
    pass


@cli.command()
@click.option(
    "--agent",
    required=True,
    help="Agent name or path, e.g. mini-swe-agent or agents/mini-swe-agent",
)
@click.option(
    "--benchmark",
    required=True,
    help=f"Name of the benchmark to run ({', '.join(BENCHMARKS)})",
)
@click.option(
    "--task_ids",
    default=None,
    help="Comma-separated list of task IDs to run (runs all tasks if not provided)",
)
@click.option(
    "--model",
    default=None,
    help="Model for the agent (default: MSWEA_MODEL_NAME)",
)
def run(agent: str, benchmark: str, task_ids: str | None, model: str | None):
    """Run an agent against a benchmark: one trial at a time, in this process (the queue comes in Phase 5)."""
    if benchmark not in BENCHMARKS:
        raise click.BadParameter(f"unknown benchmark {benchmark!r}; known: {', '.join(BENCHMARKS)}", param_hint="--benchmark")
    try:
        agent_class = resolve_agent(agent)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--agent") from None
    bench = BENCHMARKS[benchmark]()
    the_agent = agent_class(model)
    tasks = _select(bench.tasks(), task_ids)
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    run_dir = REPO_ROOT / "runs" / run_id
    limits = Limits()
    click.echo(f"Run {run_id}: {the_agent.name} ({the_agent.model}) on {benchmark}, {len(tasks)} task(s)")

    bench.setup(tasks)  # build the task images once, before any trial
    results = []
    for task in tasks:
        outcome = run_trial(bench, the_agent, task, limits, run_dir / task.id / "attempt-1")
        click.echo(f"  {task.id}: {outcome.status}")
        results.append((task.id, outcome))

    summary = {
        "run_id": run_id,
        "benchmark": benchmark,
        "agent": the_agent.name,
        "model": the_agent.model,
        "versions": {"harness": _git_sha(REPO_ROOT), "agent": _git_sha(REPO_ROOT / "agents" / the_agent.name)},
        "limits": asdict(limits),
        **_accuracy(results),
        "trials": [{"task_id": task_id, **asdict(outcome)} for task_id, outcome in results],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    _print_table(results, summary)
    click.echo(f"Saved to {run_dir}")


def _select(tasks: list[Task], task_ids: str | None) -> list[Task]:
    if not task_ids:
        return tasks
    wanted = task_ids.split(",")
    unknown = set(wanted) - {t.id for t in tasks}
    if unknown:
        raise click.BadParameter(f"unknown task ids: {', '.join(sorted(unknown))}", param_hint="--task_ids")
    return [t for t in tasks if t.id in wanted]


def _accuracy(results: list[tuple[str, Outcome]]) -> dict:
    """Accuracy = mean score_float over graded trials only; the other statuses are listed, not counted."""
    graded = [o.score_float for _, o in results if o.status == "graded"]
    excluded = {}
    for task_id, o in results:
        if o.status != "graded":
            excluded.setdefault(o.status, []).append(task_id)
    return {
        "accuracy": sum(graded) / len(graded) if graded else None,
        "graded": len(graded),
        "excluded": excluded,
        "total_cost_usd": sum(o.cost_usd for _, o in results),
    }


def _print_table(results: list[tuple[str, Outcome]], summary: dict) -> None:
    click.echo(f"\n{'task':<12}{'status':<14}{'score':<8}{'cost':>9}{'seconds':>9}")
    for task_id, o in results:
        score = "" if o.score is None else str(o.score.value)
        click.echo(f"{task_id:<12}{o.status:<14}{score:<8}{f'${o.cost_usd:.4f}':>9}{o.duration_s:>9.1f}")
    accuracy = "n/a" if summary["accuracy"] is None else f"{summary['accuracy']:.2f}"
    click.echo(f"\nAccuracy: {accuracy} over {summary['graded']} graded trial(s); total cost ${summary['total_cost_usd']:.4f}")
    for status, task_ids in summary["excluded"].items():
        click.echo(f"{status} ({len(task_ids)}, not in accuracy): {', '.join(task_ids)}")


def _git_sha(path: Path) -> str | None:
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() or None


@cli.command()
@click.option(
    "--run_id",
    required=True,
    help="The unique identifier for the benchmark run",
)
def status(run_id: str):
    """Check the status of a benchmark run."""
    click.echo(f"Checking status for run: {run_id}")

    # TODO: Implement status checking logic

    click.echo("Status: Not implemented yet")


@cli.command()
@click.option(
    "--run_id",
    required=True,
    help="The unique identifier for the benchmark run",
)
@click.option(
    "--format",
    type=click.Choice(["json", "table", "summary"], case_sensitive=False),
    default="json",
    help="Output format for results (default: json)",
)
def results(run_id: str, format: str):
    """Get results from a completed run."""
    click.echo(f"Retrieving results for run: {run_id}")
    click.echo(f"Format: {format}")

    # TODO: Implement results retrieval logic

    click.echo("Results: Not implemented yet")


def main():
    """Main entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
