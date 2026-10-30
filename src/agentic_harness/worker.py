"""Workers: take work from the queue (the trials table) and do it.

One worker = one thread with its own DB connection, running one trial (one container) at a time. Threads, not
processes: a trial spends its time waiting on `docker` and on model calls, so the GIL costs nothing.
"""

import json
import logging
import os
import socket
import sqlite3
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import closing, contextmanager
from pathlib import Path

from agentic_harness.agents import Agent, PreflightError, resolve_agent
from agentic_harness.benchmarks import BENCHMARKS, Benchmark
from agentic_harness.environments import DockerEnvironment, ensure_image
from agentic_harness.redact import redact
from agentic_harness.runner import run_trial
from agentic_harness.store import Claim, LostClaim, Store
from agentic_harness.types import Limits, Task

LEASE_S = 120  # a claim lapses this long after its last renewal
RENEW_EVERY_S = 30
MAX_ATTEMPTS = 3  # attempts per trial, counting lost workers; only a retryable infra_error is retried
RETRY_BACKOFF_S = 5  # a retry waits this times the attempt number
IDLE_SLEEP_S = 2  # how long a worker with nothing to do waits before looking again
ENVIRONMENT = DockerEnvironment  # the EnvironmentFactory every trial runs in; the one place the worker picks a backend

log = logging.getLogger("harness.worker")


def work(db_path: Path, concurrency: int = 1, exit_when_idle: bool = False, run_id: str | None = None) -> None:
    """Run `concurrency` workers until interrupted or, with exit_when_idle, until no run is left unfinished.
    With run_id, work only on that run (what `harness run --wait` does)."""
    preflight(db_path, run_id)
    prefix = f"{socket.gethostname()}-{os.getpid()}"
    threads = [
        threading.Thread(target=_loop, args=(db_path, f"{prefix}-{i}", exit_when_idle, run_id), name=f"worker-{i}", daemon=True)
        for i in range(concurrency)
    ]
    with closing(Store(db_path)) as store:
        store.worker_started(prefix)  # lets `harness status` warn when no worker is running
    try:
        with _heartbeat(db_path, lambda s: s.worker_heartbeat(prefix)):
            for t in threads:
                t.start()
            for t in threads:
                while t.is_alive():
                    t.join(0.5)  # short joins keep Ctrl-C working; a killed worker's leases lapse and its trials are reclaimed
    finally:
        with closing(Store(db_path)) as store:
            store.worker_stopped(prefix)


def preflight(db_path: Path, run_id: str | None = None) -> None:
    """Before claiming anything: every agent + model the unfinished runs (or this run) need can run at all. Raises
    PreflightError naming the run, so a missing key fails once, loudly, not as an infra_error in every trial."""
    with closing(Store(db_path)) as store:
        runs = store.unfinished_runs(run_id)
    for run in runs:
        try:
            resolve_agent(run["agent"])(run["model"]).preflight()
        except PreflightError as e:
            raise PreflightError(f"run {run['id']} ({run['agent']}, {run['model']}): {e}") from None


def _loop(db_path: Path, worker: str, exit_when_idle: bool, run_id: str | None) -> None:
    store = Store(db_path)
    while True:
        try:
            if _work_once(store, db_path, worker, run_id):
                continue
            if exit_when_idle and not store.unfinished(run_id):
                return
        except Exception:  # a harness bug: keep the worker alive; a claim it held lapses and is reclaimed
            log.exception("%s: unexpected error", worker)
        time.sleep(IDLE_SLEEP_S)


def _work_once(store: Store, db_path: Path, worker: str, run_id: str | None) -> bool:
    """Do one piece of work: a run's setup, or one attempt at a trial. False if there was nothing to do."""
    for t in store.reclaim_expired(MAX_ATTEMPTS, RETRY_BACKOFF_S):
        log.warning("trial %s: worker lost (lease expired); attempt %s closed as infra_error", t["id"], t["attempt_count"])
    if run := store.claim_setup(worker, LEASE_S, run_id):
        _setup(store, db_path, worker, run)
        return True
    if claim := store.claim_trial(worker, LEASE_S, run_id):
        _attempt(store, db_path, worker, claim)
        return True
    return False


def _setup(store: Store, db_path: Path, worker: str, run: sqlite3.Row) -> None:
    """Benchmark.setup() for the run's tasks. If it raises, the run fails with the traceback saved in setup.log."""
    benchmark = BENCHMARKS[run["benchmark"]]()
    task_ids = {t["task_id"] for t in store.trials(run["id"])}
    log.info("run %s: setting up %s", run["id"], run["benchmark"])
    error = None
    with _heartbeat(db_path, lambda s: s.renew_setup_lease(run["id"], worker, LEASE_S)):
        try:
            tasks = [t for t in benchmark.tasks() if t.id in task_ids]
            benchmark.setup(tasks)
            _prepare_images(benchmark, resolve_agent(run["agent"])(run["model"]), tasks)
        except Exception as e:
            log_file = store.runs_dir / run["id"] / "setup.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(redact(traceback.format_exc()))  # a DockerError from a build carries the build log
            error = redact(f"{type(e).__name__}: {str(e)[:300]} (full log: {log_file})")
    try:
        store.finish_setup(run["id"], worker, error)
    except LostClaim:
        log.warning("run %s: setup lease lost; result discarded", run["id"])
        return
    log.info("run %s: %s", run["id"], "setup failed: " + error if error else "set up; trials claimable")


def _prepare_images(benchmark: Benchmark, agent: Agent, tasks: list[Task]) -> None:
    """Every image a trial will start, made local now, so no trial stalls on a first pull or build: graders, MCP
    servers (pulled unless setup() built them) and the agent's own derived images (e.g. Claude Code's)."""
    for image in sorted({benchmark.grader_image(t) for t in tasks} | {s.image for t in tasks for s in benchmark.mcp_servers(t)}):
        ensure_image(image)
    for t in tasks:
        agent.prepare_image(t.image)


def _attempt(store: Store, db_path: Path, worker: str, claim: Claim) -> None:
    """One attempt at a trial; then, in one transaction, its outcome and the trial's next state (done, or re-queued)."""
    run = store.run(claim.run_id)
    benchmark = BENCHMARKS[run["benchmark"]]()
    agent = resolve_agent(run["agent"])(run["model"])
    task = next(t for t in benchmark.tasks() if t.id == claim.task_id)
    limits = Limits(**json.loads(run["config"])["limits"])

    def load_events() -> None:  # after each turn; a failure here only delays them to the attempt's end
        try:
            store.load_events(claim.attempt_id, claim.artifacts_dir / "events.jsonl")
        except sqlite3.Error as e:
            log.warning("%s attempt %s: loading events failed (%s); retried at the end", claim.task_id, claim.n, e)

    log.info("run %s: %s attempt %s", claim.run_id, claim.task_id, claim.n)
    with _heartbeat(db_path, lambda s: s.renew_lease(claim, worker, LEASE_S)):
        outcome = run_trial(benchmark, agent, task, limits, claim.artifacts_dir, ENVIRONMENT, on_turn_end=load_events)
    retry = outcome.status == "infra_error" and outcome.retryable and claim.n < MAX_ATTEMPTS
    if outcome.status == "infra_error" and not outcome.retryable:  # auth, bad request, a bug: fail fast, loudly
        log.error("%s attempt %s: infra_error, not retried: %s", claim.task_id, claim.n, (outcome.error or "").strip().rsplit("\n", 1)[-1])
    try:
        store.finish_attempt(claim, worker, outcome, retry_at=time.time() + RETRY_BACKOFF_S * claim.n if retry else None)
    except LostClaim:
        log.warning("%s attempt %s: lease lost; result (%s) discarded", claim.task_id, claim.n, outcome.status)
        return
    log.info("run %s: %s attempt %s: %s%s", claim.run_id, claim.task_id, claim.n, outcome.status, " (will retry)" if retry else "")


@contextmanager
def _heartbeat(db_path: Path, renew: Callable[[Store], bool]):
    """While the body runs, renew a lease every RENEW_EVERY_S from a background thread (with its own connection)."""
    stop = threading.Event()

    def beat() -> None:
        store = Store(db_path)
        wait = RENEW_EVERY_S
        while not stop.wait(wait):
            try:
                if not renew(store):
                    log.warning("lease lost to another worker; this attempt's result will be discarded")
                    break
                wait = RENEW_EVERY_S
            except sqlite3.OperationalError as e:  # e.g. "database is locked" past busy_timeout: retry soon
                log.warning("lease renewal failed (%s); retrying", e)
                wait = 1
        store.close()

    thread = threading.Thread(target=beat, name=f"{threading.current_thread().name}-lease", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
