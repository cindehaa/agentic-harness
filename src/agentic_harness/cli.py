"""CLI for the agentic harness system."""

import hashlib
import json
import logging
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import click

from agentic_harness import worker as worker_module
from agentic_harness.agents import resolve_agent
from agentic_harness.benchmarks import BENCHMARKS, Benchmark
from agentic_harness.paths import DB_PATH, REPO_ROOT
from agentic_harness.store import SchemaMismatch, Store
from agentic_harness.types import Limits, Task


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
@click.option("--wait", is_flag=True, help="Run the trials here (an inline worker) and print the results")
@click.option("--concurrency", default=1, show_default=True, help="With --wait: trials run at the same time")
def run(agent: str, benchmark: str, task_ids: str | None, model: str | None, wait: bool, concurrency: int):
    """Submit a run: one queued trial per task. Workers (`harness worker`) run them; --wait runs them here."""
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
    versions = {
        "harness": _git_sha(REPO_ROOT),
        "agent": _git_sha(REPO_ROOT / "agents" / the_agent.name),
        "dataset": _dataset_hash(bench),
        "model": the_agent.model,
    }
    store = _store()
    store.create_run(run_id, benchmark, the_agent.name, the_agent.model, versions, {"limits": asdict(Limits())},
                     [t.id for t in tasks])
    click.echo(f"Submitted run {run_id}: {the_agent.name} ({the_agent.model}) on {benchmark}, {len(tasks)} task(s)")
    if not wait:
        click.echo("Start a worker to run it: harness worker")
        return
    _log_to_stderr()
    worker_module.work(DB_PATH, concurrency, exit_when_idle=True, run_id=run_id)
    click.echo(f"Run {run_id} finished: harness results --run_id {run_id}")


@cli.command()
@click.option("--concurrency", default=1, show_default=True, help="Trials (containers) run at the same time")
@click.option("--exit-when-idle", is_flag=True, help="Stop once no submitted run is left unfinished")
def worker(concurrency: int, exit_when_idle: bool):
    """Take trials from the queue and run them, until Ctrl-C (or idle, with --exit-when-idle)."""
    _store().close()  # fail early on a database from another schema version
    _log_to_stderr()
    click.echo(f"Worker started: {concurrency} at a time, database {DB_PATH}", err=True)
    worker_module.work(DB_PATH, concurrency, exit_when_idle)


def _store() -> Store:
    try:
        return Store(DB_PATH)
    except SchemaMismatch as e:
        raise click.ClickException(str(e)) from None


class _EchoHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        click.echo(self.format(record), err=True)  # click finds the current stderr each time


def _log_to_stderr() -> None:
    """Show the workers' progress lines ("harness.*" loggers) on stderr."""
    logger = logging.getLogger("harness")
    if not logger.handlers:
        handler = _EchoHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(threadName)s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def _select(tasks: list[Task], task_ids: str | None) -> list[Task]:
    if not task_ids:
        return tasks
    wanted = task_ids.split(",")
    unknown = set(wanted) - {t.id for t in tasks}
    if unknown:
        raise click.BadParameter(f"unknown task ids: {', '.join(sorted(unknown))}", param_hint="--task_ids")
    return [t for t in tasks if t.id in wanted]


def _git_sha(path: Path) -> str | None:
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() or None


def _dataset_hash(bench: Benchmark) -> str:
    """Every task's fields (statements, images, ...): changes whenever the benchmark's data does."""
    tasks = json.dumps([asdict(t) for t in bench.tasks()], sort_keys=True, default=str)
    return hashlib.sha256(tasks.encode()).hexdigest()[:16]


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
