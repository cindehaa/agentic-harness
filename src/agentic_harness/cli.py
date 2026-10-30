"""CLI for the agentic harness system."""

import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
import webbrowser
from dataclasses import asdict
from pathlib import Path

import click
from dotenv import load_dotenv

from agentic_harness import worker as worker_module
from agentic_harness.agents import PreflightError, resolve_agent
from agentic_harness.benchmarks import BENCHMARKS, Benchmark
from agentic_harness.dashboard import make_server
from agentic_harness.environments import remove_images, remove_leftovers
from agentic_harness.metrics import run_metrics
from agentic_harness.paths import DB_PATH, REPO_ROOT
from agentic_harness.store import SchemaMismatch, Store
from agentic_harness.types import Limits, Task


@click.group()
def cli():
    """Agentic Harness"""
    load_dotenv(REPO_ROOT / ".env")  # API keys and default models; variables already set in the shell win


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
    help="Model for the agent (default: the agent's setting in .env, e.g. MSWEA_MODEL_NAME)",
)
@click.option("--wait", is_flag=True, help="Run the trials here (an inline worker) and print the results")
@click.option("--concurrency", default=1, show_default=True, help="With --wait: trials run at the same time")
@click.option("--trial-timeout", default=Limits().trial_timeout_s, show_default=True, help="Seconds per trial")
@click.option("--cost-limit", default=Limits().cost_usd, show_default=True, help="Model spend per trial, USD")
@click.option("--max-run-cost", type=float, default=None,
              help="Budget for the whole run, USD: once spent, no more trials start (the rest end skipped_budget)")
@click.option("--max-steps", default=Limits().max_steps, show_default=True, help="Model calls per trial")
@click.option("--memory", default=Limits().memory, show_default=True, help="Memory per container (docker --memory)")
@click.option("--cpus", default=Limits().cpus, show_default=True, help="CPUs per container (docker --cpus)")
def run(agent: str, benchmark: str, task_ids: str | None, model: str | None, wait: bool, concurrency: int,
        trial_timeout: float, cost_limit: float, max_run_cost: float | None, max_steps: int, memory: str, cpus: float):
    """Submit a run: one queued trial per task. Workers (`harness worker`) run them; --wait runs them here."""
    if benchmark not in BENCHMARKS:
        raise click.BadParameter(f"unknown benchmark {benchmark!r}; known: {', '.join(BENCHMARKS)}", param_hint="--benchmark")
    try:
        agent_class = resolve_agent(agent)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--agent") from None
    bench = BENCHMARKS[benchmark]()
    model = model or (agent_class.model_env and os.environ.get(agent_class.model_env))  # resolved now, stored in runs.model
    if not model:
        hint = f" or set {agent_class.model_env} in .env" if agent_class.model_env else ""
        raise click.BadParameter(f"no model: pass --model{hint}", param_hint="--model")
    the_agent = agent_class(model)
    try:
        the_agent.preflight()
    except PreflightError as e:
        raise click.ClickException(f"pre-flight check failed, nothing submitted: {e}") from None
    limits = Limits(trial_timeout_s=trial_timeout, cost_usd=cost_limit, max_steps=max_steps, memory=memory, cpus=cpus)
    tasks = _select(bench.tasks(), task_ids)
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    versions = {
        "harness": _git_sha(REPO_ROOT),
        "agent": the_agent.version(),
        "dataset": _dataset_hash(bench),
        "benchmark": bench.version(),  # content hash of its files, also in its image tags
        "model": the_agent.model,
    }
    store = _store()
    store.create_run(run_id, benchmark, the_agent.name, the_agent.model, versions, {"limits": asdict(limits)},
                     [t.id for t in tasks], max_run_cost)
    click.echo(f"Submitted run {run_id}: {the_agent.name} ({the_agent.model}) on {benchmark}, {len(tasks)} task(s)")
    if not wait:
        click.echo("Start a worker to run it: harness worker")
        return
    _log_to_stderr()
    if worker_module.work(DB_PATH, concurrency, exit_when_idle=True, run_id=run_id):
        click.echo(f"Interrupted: run {run_id} stays queued; `harness worker` finishes it", err=True)
        raise SystemExit(130)  # the shell's code for "stopped by Ctrl-C"
    click.echo()
    click.get_current_context().invoke(results, run_id=run_id, format="table")
    click.echo()
    click.get_current_context().invoke(results, run_id=run_id, format="summary")


@cli.command()
@click.option("--concurrency", default=1, show_default=True, help="Trials (containers) run at the same time")
@click.option("--exit-when-idle", is_flag=True, help="Stop once no submitted run is left unfinished")
def worker(concurrency: int, exit_when_idle: bool):
    """Take trials from the queue and run them, until Ctrl-C (or idle, with --exit-when-idle). Ctrl-C stops the trials
    in flight and puts them back in the queue; a second Ctrl-C exits at once."""
    _store().close()  # fail early on a database from another schema version
    _log_to_stderr()
    click.echo(f"Worker started: {concurrency} at a time, database {DB_PATH}", err=True)
    try:
        stopped = worker_module.work(DB_PATH, concurrency, exit_when_idle)
    except PreflightError as e:
        raise click.ClickException(f"pre-flight check failed, nothing claimed: {e}") from None
    if stopped:
        click.echo("Worker stopped", err=True)


@cli.command()
@click.option("--all", "remove_all", is_flag=True, help="Also remove the containers and volumes of trials still running")
@click.option("--images", is_flag=True,
              help="Also remove cached harness-* images (task and agent images) no unfinished trial needs; "
                   "they make later runs start faster")
def cleanup(remove_all: bool, images: bool):
    """Remove leftover containers and volumes (label agentic-harness=1) that belong to no running trial."""
    store = _store()
    containers, volumes, failed = remove_leftovers(set() if remove_all else store.live_attempts())
    click.echo(f"Removed {len(containers)} container(s){_names(containers)}")
    click.echo(f"Removed {len(volumes)} volume(s){_names(volumes)}")
    if images:
        tags, freed, kept = remove_images(_needed_images(store))
        click.echo(f"Removed {len(tags)} image(s), {freed / 1e9:.2f} GB freed{_names(tags)}")
        failed += [f"image {tag}: in use" for tag in kept]
    for line in failed:
        click.echo(f"Could not remove {line}", err=True)


def _names(names: list[str]) -> str:
    return ": " + ", ".join(names) if names else ""


def _needed_images(store: Store) -> set[str]:
    """Every image a trial not yet done still starts: its task's, its grader's, its MCP servers' and its agent's own."""
    needed = set()
    for run in store.unfinished_runs():
        bench, agent = BENCHMARKS[run["benchmark"]](), resolve_agent(run["agent"])(run["model"])
        waiting = {t["task_id"] for t in store.trials(run["id"]) if t["state"] != "done"}
        for task in (t for t in bench.tasks() if t.id in waiting):
            needed |= {task.image, bench.grader_image(task), agent.image_for(task.image)}
            needed |= {s.image for s in bench.mcp_servers(task)}
    return needed


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
@click.option("--run_id", default=None, help="Show this run's trials (default: list active and recent runs)")
@click.option("--all", "show_all", is_flag=True, help="List every run, not only active ones and those finished in the last hour")
def status(run_id: str | None, show_all: bool):
    """Check the status of benchmark runs, or of one run's trials."""
    store = _store()
    if run_id is None:
        runs = store.runs(finished_since=None if show_all else time.time() - 3600)
        if not runs:
            click.echo("No active or recent runs (--all lists every run)")
            return
        _table(("RUN", "BENCHMARK", "AGENT", "PROGRESS", "SPEND", "ELAPSED", "STARTED"),
               [(r["id"], r["benchmark"], r["agent"], _progress(r, r["done"], r["total"], r["skipped"]),
                 _spend(r["cost_usd"], r["max_cost_usd"]), _elapsed(r), _time(r["created_at"])) for r in runs])
    else:
        run = _find_run(store, run_id)
        trials = store.trials(run_id)
        progress = _progress(run, sum(t["state"] == "done" for t in trials), len(trials),
                             sum(t["outcome"] == "skipped_budget" for t in trials))
        click.echo(f"Run {run_id}: {run['benchmark']} · {run['agent']} ({run['model']}), {progress}, "
                   f"{_spend(sum(t['cost_usd'] for t in trials), run['max_cost_usd'])} spent, {_elapsed(run)} elapsed\n")
        _table(("TASK", "STATE", "OUTCOME", "SCORE", "ATTEMPTS", "COST", "TIME"),
               [(t["task_id"], t["state"], t["outcome"] or "", _score(t["score_value"]), str(t["attempt_count"]),
                 f"${t['cost_usd']:.4f}", _trial_time(t)) for t in trials])
    if (waiting := store.waiting_trials()) and _no_worker(store):
        click.echo(f"\n{waiting} pending — no worker running; start one with `harness worker`")


@cli.command()
@click.option(
    "--run_id",
    required=True,
    help="The unique identifier for the benchmark run",
)
@click.option(
    "--format",
    type=click.Choice(["json", "table", "summary"], case_sensitive=False),
    default="summary",
    help="Output format for results (default: summary)",
)
def results(run_id: str, format: str):
    """Get results from a run (a run still in progress shows what is done so far)."""
    store = _store()
    run = _find_run(store, run_id)
    trials = store.trials(run_id)
    accuracy, graded = store.accuracy(run_id)
    excluded = {s: [t["task_id"] for t in trials if t["outcome"] == s] for s in EXCLUDED}
    attempts = store.attempts(run_id)
    m = run_metrics(run, trials, attempts)
    if format == "json":
        report = {
            "run": {**dict(run), "versions": json.loads(run["versions"]), "config": json.loads(run["config"])},
            "accuracy": accuracy,
            "graded": graded,
            "excluded": excluded,
            **m,
            "trials": [
                {**dict(t), "score_value": _parse(t["score_value"]),
                 "attempts": [{**dict(a), "score": _parse(a["score"])} for a in attempts if a["trial_id"] == t["id"]]}
                for t in trials
            ],
        }
        click.echo(json.dumps(report, indent=2))
    elif format == "table":
        _table(("TASK", "OUTCOME", "SCORE", "COST", "TOKENS IN/OUT", "AGENT", "GRADING", "TIME", "ATTEMPTS"),
               [(t["task_id"], t["outcome"] or t["state"], _score(t["score_value"]), f"${t['cost_usd']:.4f}",
                 f"{t['input_tokens']:,}/{t['output_tokens']:,}", f"{t['agent_s']:.1f}s", f"{t['grading_s']:.1f}s",
                 f"{t['duration_s']:.1f}s", str(t["attempt_count"])) for t in trials])
    else:
        done = sum(t["state"] == "done" for t in trials)
        click.echo(f"{run['benchmark']} · {run['agent']} ({run['model']})   run {run_id}")
        if done < len(trials):
            click.echo(f"In progress: {_progress(run, done, len(trials))}")
        click.echo("Accuracy: " + ("n/a" if accuracy is None else f"{accuracy:.2f}") + f" (n={graded} graded, {m['passed']} passed)")
        click.echo("Excluded: " + " · ".join(f"{s} {len(ids)}" for s, ids in excluded.items()))
        for s, ids in excluded.items():
            if ids:
                click.echo(f"  {s}: {', '.join(ids)}")
        click.echo(f"Cost: {_spend(m['cost_usd'], m['max_cost_usd'])} · {_usd(m['cost_per_task_usd'])}/task · "
                   f"{_usd(m['cost_per_passing_task_usd'])}/passing task")
        click.echo(f"Tokens: {m['input_tokens']:,} in · {m['output_tokens']:,} out · {m['model_calls']:,} model calls")
        click.echo(f"Time per trial (avg): env start {_secs(m['avg_env_start_s'])} · agent {_secs(m['avg_agent_s'])} · "
                   f"grading {_secs(m['avg_grading_s'])}")
        click.echo(f"Run: setup {_secs(m['setup_s'])} · wall {_secs(m['wall_s'])} (first trial start to last end) · "
                   f"queue wait {_secs(m['total_queue_wait_s'])} total · "
                   + ("n/a" if m["trials_per_min"] is None else f"{m['trials_per_min']:.1f}") + " trials/min")


EXCLUDED = ("grader_error", "infra_error", "unsupported", "skipped_budget")  # left out of accuracy, always reported


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _spend(cost: float, budget: float | None) -> str:
    return f"${cost:.4f}" + ("" if budget is None else f" of ${budget:.2f}")


def _secs(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}s"


def _elapsed(run) -> str:
    """Since submission: until it finished, or until now."""
    return _secs((run["finished_at"] or time.time()) - run["created_at"])


def _trial_time(trial) -> str:
    """Its finished attempts' time, plus the one in flight so far."""
    running = time.time() - trial["running_since"] if trial["running_since"] else 0
    return _secs(trial["duration_s"] + running)


def _find_run(store: Store, run_id: str):
    run = store.run(run_id)
    if run is None:
        raise click.ClickException(f"no run {run_id!r} (harness status --all lists every run)")
    return run


def _progress(run, done: int, total: int, skipped: int = 0) -> str:
    if run["status"] == "failed":
        return "setup failed"
    if run["status"] == "pending":
        return "setting up" if run["setup_claimed_by"] else "queued"
    return f"{done}/{total} done" + (f" (budget reached: {skipped} skipped)" if skipped else "")


def _no_worker(store: Store) -> bool:
    """No worker has sent a heartbeat within a lease (it renews every 30 s); a running trial's lease then lapses too,
    so it counts as pending."""
    return store.live_workers(time.time() - worker_module.LEASE_S) == 0


def _score(score_value: str | None) -> str:
    return "" if score_value is None else str(json.loads(score_value))


def _parse(value: str | None):
    return None if value is None else json.loads(value)


def _time(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]
    for row in (headers, *rows):
        click.echo("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip())


@cli.command()
@click.option("--port", default=8000, show_default=True, help="Port on 127.0.0.1")
@click.option("--open/--no-open", "open_browser", default=True, show_default=True, help="Open it in the browser")
def dashboard(port: int, open_browser: bool):
    """A live, read-only web view of runs, trials and traces, until Ctrl-C."""
    try:
        server = make_server(DB_PATH, port)
    except OSError as e:
        raise click.ClickException(f"can't listen on port {port} ({e.strerror}): pass another --port") from None
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    click.echo(f"Dashboard: {url} (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    cli()


if __name__ == "__main__":
    main()
