import hashlib
import json
import logging
import subprocess
import time
import uuid
import webbrowser
from dataclasses import asdict
from pathlib import Path

import click
from click.core import ParameterSource
from dotenv import load_dotenv

from agentic_harness import parity as parity_module
from agentic_harness import validate as validate_module
from agentic_harness import worker as worker_module
from agentic_harness.agents import MODEL_ENV, Agent, PreflightError, resolve_agent, resolve_model
from agentic_harness.benchmarks import BENCHMARKS, Benchmark, BenchmarkUnavailable
from agentic_harness.dashboard import make_server
from agentic_harness.environments import remove_images, remove_leftovers
from agentic_harness.metrics import run_metrics
from agentic_harness.paths import DB_PATH, REPO_ROOT, RUNS_DIR
from agentic_harness.store import SchemaMismatch, Store
from agentic_harness.types import LimitOverrides, Limits, Task

MAX_RUNNING_ENV = "HARNESS_MAX_RUNNING"
_DEFAULT = "  [default: the benchmark's own, else %s]"


@click.group()
def cli():
    """Agentic Harness"""
    load_dotenv(REPO_ROOT / ".env")  # variables already set in the shell win


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
    help=f"Model, litellm-style, e.g. anthropic/claude-haiku-5-5 (default: {MODEL_ENV} in .env)",
)
@click.option("--wait", is_flag=True, help="Run the trials here (an inline worker) and print the results")
@click.option("--concurrency", default=1, show_default=True, help="With --wait: trials run at the same time")
@click.option("--max-running", type=click.IntRange(min=1), default=None, envvar=MAX_RUNNING_ENV, show_envvar=True,
              help="With --wait: cap on trials running at once across ALL workers")
# limits unset unless given, so a benchmark's own values stand
@click.option("--trial-timeout", type=float, default=None, help=f"Seconds per trial{_DEFAULT % Limits().trial_timeout_s}")
@click.option("--grade-timeout", type=float, default=None, help=f"Seconds to grade a trial{_DEFAULT % Limits().grade_timeout_s}")
@click.option("--cost-limit", type=float, default=None, help=f"Model spend per trial, USD{_DEFAULT % Limits().cost_usd}")
@click.option("--max-run-cost", type=float, default=None,
              help="Budget for the whole run, USD: once spent, no more trials start (the rest end skipped_budget)")
@click.option("--max-steps", type=int, default=None, help=f"Model calls per trial{_DEFAULT % Limits().max_steps}")
@click.option("--memory", type=str, default=None, help=f"Memory per container (docker --memory){_DEFAULT % Limits().memory}")
@click.option("--cpus", type=float, default=None, help=f"CPUs per container (docker --cpus){_DEFAULT % Limits().cpus}")
def run(agent: str, benchmark: str, task_ids: str | None, model: str | None, wait: bool, concurrency: int,
        max_running: int | None, trial_timeout: float | None, grade_timeout: float | None, cost_limit: float | None,
        max_run_cost: float | None, max_steps: int | None, memory: str | None, cpus: float | None):
    """Submit a run: one queued trial per task. Workers (`harness worker`) run them; --wait runs them here."""
    for option in ("concurrency", "max_running"):  # only flags: HARNESS_MAX_RUNNING from the environment is fine
        if not wait and click.get_current_context().get_parameter_source(option) is ParameterSource.COMMANDLINE:
            flag = "--" + option.replace("_", "-")
            raise click.UsageError(f"{flag} applies only with --wait; without it, workers run the trials: "
                                   f"harness worker {flag} N")
    bench, the_agent = _benchmark_and_agent(benchmark, agent, model)
    flags = LimitOverrides(trial_timeout_s=trial_timeout, grade_timeout_s=grade_timeout, cost_usd=cost_limit,
                           max_steps=max_steps, memory=memory, cpus=cpus)
    run_id = _submit(bench, the_agent, _select(bench, task_ids), flags, max_run_cost)
    if not wait:
        click.echo("Start a worker to run it: harness worker")
        return
    _log_to_stderr()
    if worker_module.work(DB_PATH, concurrency, exit_when_idle=True, run_id=run_id, max_running=max_running):
        click.echo(f"Interrupted: run {run_id} stays queued; `harness worker` finishes it", err=True)
        raise SystemExit(130)
    click.echo()
    click.get_current_context().invoke(results, run_id=run_id, format="table")
    click.echo()
    click.get_current_context().invoke(results, run_id=run_id, format="summary")


def _benchmark_and_agent(benchmark: str, agent: str, model: str | None) -> tuple[Benchmark, Agent]:
    if benchmark not in BENCHMARKS:
        raise click.BadParameter(f"unknown benchmark {benchmark!r}; known: {', '.join(BENCHMARKS)}", param_hint="--benchmark")
    try:
        agent_class = resolve_agent(agent)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--agent") from None
    model = resolve_model(model)
    if not model:
        raise click.BadParameter(f"no model: pass --model or set {MODEL_ENV} in .env", param_hint="--model")
    the_agent = agent_class(model)
    try:
        the_agent.preflight()
    except PreflightError as e:
        raise click.ClickException(f"pre-flight check failed, nothing submitted: {e}") from None
    return BENCHMARKS[benchmark](), the_agent


def _submit(bench: Benchmark, the_agent: Agent, tasks: list[Task], flags: LimitOverrides, max_run_cost: float | None) -> str:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    versions = {
        "harness": _git_sha(REPO_ROOT),
        "agent": the_agent.version(),
        "dataset": _dataset_hash(bench),
        "benchmark": bench.version(),
        "model": the_agent.model,
    }
    store = _store()
    config = {"limits": asdict(Limits()), "limit_flags": flags.given(), "non_standard_limits": _non_standard(bench, tasks, flags)}
    store.create_run(run_id, bench.name, the_agent.name, the_agent.model, versions, config, [t.id for t in tasks], max_run_cost)
    click.echo(f"Submitted run {run_id}: {the_agent.name} ({the_agent.model}) on {bench.name}, {len(tasks)} task(s)")
    if config["non_standard_limits"]:
        click.echo(f"Note: {_non_standard_note(config)}")
    return run_id


def _non_standard(bench: Benchmark, tasks: list[Task], flags: LimitOverrides) -> list[str]:
    declared = [bench.limits(t).given() for t in tasks]
    return sorted({name for name, value in flags.given().items() for d in declared if name in d and d[name] != value})


def _non_standard_note(config: dict) -> str | None:
    if names := config.get("non_standard_limits"):
        return f"non-standard limits: {', '.join(names)} set by flag over the benchmark's own value"
    return None


@cli.command()
@click.option("--benchmark", required=True, help=f"Name of the benchmark to check ({', '.join(BENCHMARKS)})")
@click.option("--task_ids", default=None, help="Comma-separated task IDs to check (all if not provided)")
@click.option("--concurrency", default=4, show_default=True, help="Trials (containers) run at the same time")
def validate(benchmark: str, task_ids: str | None, concurrency: int):
    """Check an adapter is wired correctly, with no real agent: doing nothing must fail every task, and the task's
    reference solution must pass it. Exits 1 if any check fails."""
    if benchmark not in BENCHMARKS:
        raise click.BadParameter(f"unknown benchmark {benchmark!r}; known: {', '.join(BENCHMARKS)}", param_hint="--benchmark")
    bench = BENCHMARKS[benchmark]()
    tasks = _select(bench, task_ids)
    out_dir = RUNS_DIR / "validate" / f"{benchmark}-{time.strftime('%Y%m%d-%H%M%S')}"
    checks = validate_module.validate(bench, tasks, out_dir, concurrency)
    by_task = {t.id: {c.kind: c for c in checks if c.task_id == t.id} for t in tasks}
    _table(("TASK", "DO NOTHING (must fail)", "REFERENCE SOLUTION (must pass)", "SOLUTION BY"),
           [(task_id, _verdict(c["null"]), _verdict(c["oracle"]), c["oracle"].source or "-") for task_id, c in by_task.items()])
    failed = [c for c in checks if c.ok is False]
    for c in failed:
        click.echo(f"\n{c.task_id} {'do-nothing' if c.kind == 'null' else 'reference'} check: {c.detail}")
    nulls, oracles = [c for c in checks if c.kind == "null"], [c for c in checks if c.kind == "oracle" and c.ok is not None]
    sources = sorted({c.source for c in oracles})
    click.echo(f"\n{benchmark}: do-nothing {sum(c.ok for c in nulls)}/{len(nulls)} failed as they should; reference "
               f"{sum(c.ok for c in oracles)}/{len(oracles)} passed ({', '.join(sources) or 'none'}); "
               f"{len(nulls) - len(oracles)} task(s) without a reference solution (do-nothing check only)")
    if "harness-written" in sources:
        click.echo("Note: harness-written reference solutions check the wiring, not the benchmark (we wrote them)")
    click.echo(f"Artifacts: {out_dir}")
    if failed:
        raise SystemExit(1)


@cli.command()
@click.option("--benchmark", required=True, help=f"Name of the benchmark ({', '.join(BENCHMARKS)})")
@click.option("--agent", required=True, help="Agent name, e.g. mini-swe-agent")
@click.option("--runs", default=3, show_default=True, help="Runs per side")
@click.option("--task_ids", default=None, help="Comma-separated task IDs (all if not provided)")
@click.option("--model", default=None, help=f"Model, litellm-style (default: {MODEL_ENV} in .env)")
@click.option("--concurrency", default=4, show_default=True, help="Trials run at the same time, per side")
def parity(benchmark: str, agent: str, runs: int, task_ids: str | None, model: str | None, concurrency: int):
    """Does our harness score the agent the same as the benchmark's original workflow? Runs the agent N times each
    way; the two sides' run-score ranges must overlap. Writes runs/parity/<benchmark>-<timestamp>/parity.json."""
    bench, the_agent = _benchmark_and_agent(benchmark, agent, model)
    tasks = _select(bench, task_ids)
    out_dir = RUNS_DIR / "parity" / f"{benchmark}-{time.strftime('%Y%m%d-%H%M%S')}"
    _log_to_stderr()
    run_ids, harness_scores = [], []
    for _ in range(runs):
        run_id = _submit(bench, the_agent, tasks, LimitOverrides(), None)
        if worker_module.work(DB_PATH, concurrency, exit_when_idle=True, run_id=run_id):
            raise SystemExit(130)
        run_ids.append(run_id)
        harness_scores.append({t["task_id"]: t["score_float"] if t["outcome"] == "graded" else None for t in _store().trials(run_id)})
    harness_cost = sum(t["cost_usd"] for r in run_ids for t in _store().trials(r))
    click.echo(f"Original workflow: {runs} run(s) of {len(tasks)} task(s)", err=True)
    original_scores, original_cost, errors = parity_module.original_side(
        bench, tasks, the_agent.name, the_agent.model, runs, out_dir / "original", concurrency)
    sides = {"harness": parity_module.summarize(harness_scores), "original": parity_module.summarize(original_scores)}
    report = {
        "benchmark": benchmark, "agent": the_agent.name, "model": the_agent.model, "runs": runs,
        "tasks": [t.id for t in tasks], "harness_run_ids": run_ids, **sides,
        "overlap": parity_module.overlap(sides["harness"]["range"], sides["original"]["range"]),
        "pass_rates": {"harness": parity_module.pass_rates(harness_scores), "original": parity_module.pass_rates(original_scores)},
        "scores": {"harness": harness_scores, "original": original_scores},
        "cost_usd": {"harness": harness_cost, "original": original_cost},
        "original_errors": errors,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "parity.json").write_text(json.dumps(report, indent=2))
    click.echo()
    _table(("SIDE", "MEAN ± SEM", "RUN ACCURACIES", "COST"),
           [(side, _mean_sem(s), ", ".join(f"{a:.2f}" for a in s["run_accuracies"]), f"${report['cost_usd'][side]:.4f}")
            for side, s in sides.items()])
    click.echo()
    _table(("TASK", "HARNESS PASS RATE", "ORIGINAL PASS RATE"),
           [(t.id, _rate(report["pass_rates"]["harness"][t.id]), _rate(report["pass_rates"]["original"][t.id])) for t in tasks])
    for e in errors:
        click.echo(f"original workflow error (left out): {e}")
    verdict = {True: "ranges overlap: no evidence of an adaptation error",
               False: "ranges DO NOT overlap: treat as an adaptation error until shown otherwise",
               None: "not comparable: a side has no scored run"}[report["overlap"]]
    click.echo(f"\nParity: {verdict}\nReport: {out_dir / 'parity.json'}")


def _mean_sem(side: dict) -> str:
    if side["mean"] is None:
        return "n/a"
    return f"{side['mean']:.3f}" + ("" if side["sem"] is None else f" ± {side['sem']:.3f}")


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _verdict(check) -> str:
    return "-" if check.ok is None else ("ok" if check.ok else "FAIL")


SKILL = REPO_ROOT / ".claude" / "skills" / "add-benchmark" / "SKILL.md"
ADAPT_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash(.venv/bin/harness validate:*)",
               "Bash(.venv/bin/python -m pytest:*)", "Bash(docker image inspect:*)", "Bash(docker images:*)", "Bash(ls:*)"]


@cli.command()
@click.argument("benchmark_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--tasks", "n_tasks", default=3, show_default=True, help="How many (simple) tasks to adapt")
@click.option("--name", default=None, help="Registry name for the benchmark (default: the folder's name)")
@click.option("--model", default=None,
              help="Model for the drafting Claude Code: litellm-style or Claude Code's own name, e.g. sonnet "
                   "(default: Claude Code's own)")
@click.option("--max-budget-usd", default=3.0, show_default=True, help="Claude Code's spend limit")
def adapt(benchmark_dir: Path, n_tasks: int, name: str | None, model: str | None, max_budget_usd: float):
    """Draft an adapter for BENCHMARK_DIR with Claude Code (headless, following the add-benchmark skill), then run
    `harness validate` on it. The draft needs human review before its scores are trusted."""
    name = name or benchmark_dir.resolve().name
    folder = benchmark_dir.resolve().relative_to(REPO_ROOT)
    prompt = (f"{SKILL.read_text()}\n\n---\n\nAdd an adapter for the benchmark in `{folder}`, registered as "
              f"`{name}`. Adapt only {n_tasks} simple task(s) (few dependencies, quick to build and grade); "
              f"name them in the adapter. Follow the skill above exactly, and iterate until "
              f"`.venv/bin/harness validate --benchmark {name}` passes. Do not commit. You run headless: nobody "
              f"can answer questions, so make the most faithful choice and list it in your report.")
    argv = ["claude", "-p", "--permission-mode", "acceptEdits", "--allowedTools", *ADAPT_TOOLS,
            "--disallowedTools", "WebFetch", "WebSearch", "--max-budget-usd", str(max_budget_usd),
            *(["--model", resolve_agent("claude-code")(model).native_model()] if model else [])]
    click.echo(f"Drafting the {name} adapter with Claude Code (budget ${max_budget_usd:.2f})...", err=True)
    drafted = subprocess.run(argv, cwd=REPO_ROOT, input=prompt, text=True)  # on stdin: the skill starts with "---", an option to argv
    if drafted.returncode != 0:
        raise click.ClickException(f"Claude Code exited {drafted.returncode}")
    click.echo(f"\nValidating {name}...")
    # a new process: this one imported the registry before the adapter existed
    checked = subprocess.run([str(REPO_ROOT / ".venv" / "bin" / "harness"), "validate", "--benchmark", name], cwd=REPO_ROOT)
    click.echo(f"\nDraft {'PASSES' if checked.returncode == 0 else 'FAILS'} validate. Review the diff (git diff, "
               f"git status) before trusting its scores: a drafted adapter needs human review.")
    raise SystemExit(checked.returncode)


@cli.command()
@click.option("--concurrency", default=1, show_default=True, help="Trials (containers) run at the same time")
@click.option("--exit-when-idle", is_flag=True, help="Stop once no submitted run is left unfinished")
@click.option("--max-running", type=click.IntRange(min=1), default=None, envvar=MAX_RUNNING_ENV, show_envvar=True,
              help="Cap on trials running at once across ALL workers (default: no cap)")
def worker(concurrency: int, exit_when_idle: bool, max_running: int | None):
    """Take trials from the queue and run them, until Ctrl-C (or idle, with --exit-when-idle). Ctrl-C stops the trials
    in flight and puts them back in the queue; a second Ctrl-C exits at once."""
    _store().close()
    _log_to_stderr()
    cap = f", at most {max_running} running across all workers" if max_running else ""
    click.echo(f"Worker started: {concurrency} at a time{cap}, database {DB_PATH}", err=True)
    stopped = worker_module.work(DB_PATH, concurrency, exit_when_idle, max_running=max_running)
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
    needed = set()
    for run in store.unfinished_runs():
        bench, agent = BENCHMARKS[run["benchmark"]](), resolve_agent(run["agent"])(run["model"])
        waiting = {t["task_id"] for t in store.trials(run["id"]) if t["state"] != "done"}
        try:
            tasks = bench.tasks()
        except BenchmarkUnavailable:
            continue
        for task in (t for t in tasks if t.id in waiting):
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
    logger = logging.getLogger("harness")
    if not logger.handlers:
        handler = _EchoHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(threadName)s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def _select(bench: Benchmark, task_ids: str | None) -> list[Task]:
    try:
        tasks = bench.tasks()
    except BenchmarkUnavailable as e:
        raise click.ClickException(str(e)) from None
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
                 "attempts": [{**dict(a), "score": _parse(a["score"]), "limits": _parse(a["limits"])}
                              for a in attempts if a["trial_id"] == t["id"]]}
                for t in trials
            ],
        }
        click.echo(json.dumps(report, indent=2))
    elif format == "table":
        if note := _non_standard_note(json.loads(run["config"])):
            click.echo(f"Note: {note}\n")
        _table(("TASK", "OUTCOME", "SCORE", "COST", "TOKENS IN/OUT", "AGENT", "GRADING", "TIME", "ATTEMPTS"),
               [(t["task_id"], t["outcome"] or t["state"], _score(t["score_value"]), f"${t['cost_usd']:.4f}",
                 f"{t['input_tokens']:,}/{t['output_tokens']:,}", f"{t['agent_s']:.1f}s", f"{t['grading_s']:.1f}s",
                 f"{t['duration_s']:.1f}s", str(t["attempt_count"])) for t in trials])
    else:
        done = sum(t["state"] == "done" for t in trials)
        click.echo(f"{run['benchmark']} · {run['agent']} ({run['model']})   run {run_id}")
        if done < len(trials):
            click.echo(f"In progress: {_progress(run, done, len(trials))}")
        if note := _non_standard_note(json.loads(run["config"])):
            click.echo(f"Note: {note}")
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


EXCLUDED = ("grader_error", "infra_error", "unsupported", "skipped_budget")


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _spend(cost: float, budget: float | None) -> str:
    return f"${cost:.4f}" + ("" if budget is None else f" of ${budget:.2f}")


def _secs(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}s"


def _elapsed(run) -> str:
    return _secs((run["finished_at"] or time.time()) - run["created_at"])


def _trial_time(trial) -> str:
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
