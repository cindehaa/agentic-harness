"""Workers: take work from the queue (the trials table) and do it.

One worker = one thread with its own DB connection, running one trial (one container) at a time. Threads, not
processes: a trial spends its time waiting on `docker` and on model calls, so the GIL costs nothing.
"""

import json
import logging
import os
import signal
import socket
import sqlite3
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import closing, contextmanager
from pathlib import Path

from agentic_harness.agents import Agent, AgentStats, PreflightError, resolve_agent
from agentic_harness.benchmarks import BENCHMARKS, Benchmark
from agentic_harness.environments import ATTEMPT_LABEL, DockerEnvironment, Environment, EnvironmentLost, ensure_image
from agentic_harness.redact import redact
from agentic_harness.runner import run_trial
from agentic_harness.store import Claim, LostClaim, Store
from agentic_harness.types import Limits, Outcome, Task

LEASE_S = 120  # a claim lapses this long after its last renewal
RENEW_EVERY_S = 30
MAX_ATTEMPTS = 3  # attempts per trial, counting lost workers; only a retryable infra_error is retried
RETRY_BACKOFF_S = 5  # a retry waits this times the attempt number
IDLE_SLEEP_S = 2  # how long a worker with nothing to do waits before looking again
STOP_GRACE_S = 30  # after Ctrl-C: how long to wait for the trial threads to notice their worlds are gone
ENVIRONMENT = DockerEnvironment  # the EnvironmentFactory every trial runs in; the one place the worker picks a backend

log = logging.getLogger("harness.worker")


class Stop:
    """Ctrl-C (SIGINT) or SIGTERM: stop claiming, end the trials in flight and hand them back to the queue. A second
    signal forces the exit (KeyboardInterrupt); leases then lapse and another worker reclaims the trials as usual."""

    def __init__(self) -> None:
        self.requested = threading.Event()
        self._lock = threading.Lock()
        self._attempts: dict[int, tuple[Claim, str, list[Environment]]] = {}  # attempt id -> claim, worker, its worlds
        self._setups: dict[str, str] = {}  # run id -> worker running its setup

    def handle(self, signum: int, frame) -> None:
        if self.requested.is_set():
            raise KeyboardInterrupt
        self.requested.set()
        log.warning("%s: stopping; trials in flight go back to the queue (again to force)", signal.Signals(signum).name)

    def track(self, claim: Claim, worker: str) -> None:
        with self._lock:
            self._attempts[claim.attempt_id] = (claim, worker, [])

    def track_world(self, claim: Claim, env: Environment) -> None:
        with self._lock:
            self._attempts[claim.attempt_id][2].append(env)

    def untrack(self, claim: Claim) -> None:
        with self._lock:
            self._attempts.pop(claim.attempt_id, None)

    def track_setup(self, run_id: str, worker: str | None) -> None:
        with self._lock:
            if worker is None:
                self._setups.pop(run_id, None)
            else:
                self._setups[run_id] = worker

    def stop_worlds(self) -> None:
        """Stop every world in flight (with its sidecars and volume). The trial threads' next command then fails."""
        with self._lock:
            worlds = [env for _, _, envs in self._attempts.values() for env in envs]
        for env in worlds:
            env.stop()

    def hand_back(self, store: Store) -> None:
        """Close each attempt in flight as a retryable infra_error and re-queue its trial at once (another worker takes
        it; it still counts as an attempt), and release each setup this worker held. A trial thread that finishes
        later finds it no longer holds its claim, and its result is discarded."""
        with self._lock:
            attempts, setups = list(self._attempts.values()), dict(self._setups)
        for claim, worker, _ in attempts:
            _interrupted(store, claim, worker)
        for run_id, worker in setups.items():
            store.release_setup(run_id, worker)


def _interrupted(store: Store, claim: Claim, worker: str) -> None:
    try:
        store.finish_attempt(claim, worker, Outcome("infra_error", error="worker interrupted", retryable=True),
                             retry_at=time.time())
        log.warning("%s attempt %s: worker interrupted; back in the queue", claim.task_id, claim.n)
    except LostClaim:
        pass  # already handed back, finished or reclaimed


def work(db_path: Path, concurrency: int = 1, exit_when_idle: bool = False, run_id: str | None = None,
         max_running: int | None = None) -> bool:
    """Run `concurrency` workers until stopped (Ctrl-C, SIGTERM) or, with exit_when_idle, until no run is left
    unfinished. With run_id, work only on that run (what `harness run --wait` does). With max_running, claim a trial
    only while fewer than that run across all workers. True if it was stopped."""
    prefix = f"{socket.gethostname()}-{os.getpid()}"
    stop = Stop()
    checks = Preflight()
    threads = [
        threading.Thread(target=_loop, args=(db_path, f"{prefix}-{i}", exit_when_idle, run_id, stop, checks, max_running), name=f"worker-{i}", daemon=True)
        for i in range(concurrency)
    ]
    with closing(Store(db_path)) as store:
        store.worker_started(prefix)  # lets `harness status` warn when no worker is running
    handlers = _on_signals(stop.handle)
    try:
        with _heartbeat(db_path, lambda s: s.worker_heartbeat(prefix)):
            for t in threads:
                t.start()
            while any(t.is_alive() for t in threads) and not stop.requested.is_set():
                time.sleep(0.2)  # not one long join: a signal handler runs only once the main thread is back in Python
            if stop.requested.is_set():
                _stop_gracefully(db_path, stop, threads)
    finally:
        _on_signals(handlers)
        with closing(Store(db_path)) as store:
            store.worker_stopped(prefix)
    return stop.requested.is_set()


def _on_signals(handler) -> dict:
    """Install `handler` (a function, or the dict this returned before) for SIGINT and SIGTERM; returns the old ones.
    Python only allows it in the main thread: elsewhere (e.g. a test running work() in a thread) nothing changes."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, handler[sig] if isinstance(handler, dict) else handler)
    return old


def _stop_gracefully(db_path: Path, stop: Stop, threads: list[threading.Thread]) -> None:
    stop.stop_worlds()
    with closing(Store(db_path)) as store:
        stop.hand_back(store)
    deadline = time.monotonic() + STOP_GRACE_S
    while any(t.is_alive() for t in threads) and time.monotonic() < deadline:
        stop.stop_worlds()  # again: a world started just as the signal came
        for t in threads:
            t.join(0.2)


class Preflight:
    """Each run is checked once per worker process, before any of its work is claimed: its benchmark is known and
    its agent + model can run here at all. A run that can't fails on its own, once, loudly (not as an infra_error
    in every trial), and the other runs keep flowing."""

    def __init__(self) -> None:
        self._checked: set[str] = set()
        self._lock = threading.Lock()  # held through the check: no thread claims a run's work while it is checked

    def check_new_runs(self, store: Store, run_id: str | None = None) -> None:
        with self._lock:
            for run in store.unfinished_runs(run_id):
                if run["id"] in self._checked:
                    continue
                self._checked.add(run["id"])
                if (error := preflight_error(run)) and store.fail_run(run["id"], error):
                    log.error("run %s failed: %s", run["id"], error)


def preflight_error(run: sqlite3.Row) -> str | None:
    """Why this run can't run here, or None."""
    if run["benchmark"] not in BENCHMARKS:
        return f"unknown benchmark {run['benchmark']!r}"
    try:
        resolve_agent(run["agent"])(run["model"]).preflight()
    except (ValueError, ImportError, PreflightError) as e:  # ValueError: an agent name not in the registry
        return f"pre-flight check failed ({run['agent']}, {run['model']}): {e}"
    return None


def _loop(db_path: Path, worker: str, exit_when_idle: bool, run_id: str | None, stop: Stop, checks: Preflight,
          max_running: int | None) -> None:
    store = Store(db_path)
    while not stop.requested.is_set():
        try:
            checks.check_new_runs(store, run_id)
            if _work_once(store, db_path, worker, run_id, stop, max_running):
                continue
            if exit_when_idle and not store.unfinished(run_id):
                return
        except Exception:  # a harness bug: keep the worker alive; a claim it held lapses and is reclaimed
            log.exception("%s: unexpected error", worker)
        stop.requested.wait(IDLE_SLEEP_S)


def _work_once(store: Store, db_path: Path, worker: str, run_id: str | None, stop: Stop,
               max_running: int | None = None) -> bool:
    """Do one piece of work: a run's setup, or one attempt at a trial. False if there was nothing to do."""
    for t in store.reclaim_expired(MAX_ATTEMPTS, RETRY_BACKOFF_S):
        log.warning("trial %s: worker lost (lease expired); attempt %s closed as infra_error", t["id"], t["attempt_count"])
    if run := store.claim_setup(worker, LEASE_S, run_id):
        stop.track_setup(run["id"], worker)
        try:
            _setup(store, db_path, worker, run, stop)
        finally:
            stop.track_setup(run["id"], None)
        return True
    if claim := store.claim_trial(worker, LEASE_S, run_id, max_running):
        stop.track(claim, worker)
        try:
            _attempt(store, db_path, worker, claim, stop)
        finally:
            stop.untrack(claim)
        return True
    return False


def _setup(store: Store, db_path: Path, worker: str, run: sqlite3.Row, stop: Stop) -> None:
    """Benchmark.setup() for the run's tasks. If it raises, the run fails with the traceback saved in setup.log."""
    task_ids = {t["task_id"] for t in store.trials(run["id"])}
    log.info("run %s: setting up %s", run["id"], run["benchmark"])
    error = None
    with _heartbeat(db_path, lambda s: s.renew_setup_lease(run["id"], worker, LEASE_S)):
        try:
            benchmark = BENCHMARKS[run["benchmark"]]()  # in here: an unknown name fails the run, not the worker
            tasks = [t for t in benchmark.tasks() if t.id in task_ids]
            benchmark.setup(tasks)
            prepare_images(benchmark, resolve_agent(run["agent"])(run["model"]), tasks)
        except Exception as e:
            log_file = store.runs_dir / run["id"] / "setup.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(redact(traceback.format_exc()))  # a DockerError from a build carries the build log
            error = redact(f"{type(e).__name__}: {str(e)[:300]} (full log: {log_file})")
    if stop.requested.is_set():  # Ctrl-C also reaches `docker build`: a build it broke is no reason to fail the run
        store.release_setup(run["id"], worker)
        return
    try:
        store.finish_setup(run["id"], worker, error)
    except LostClaim:
        log.warning("run %s: setup lease lost; result discarded", run["id"])
        return
    log.info("run %s: %s", run["id"], "setup failed: " + error if error else "set up; trials claimable")


def prepare_images(benchmark: Benchmark, agent: Agent, tasks: list[Task]) -> None:
    """Every image a trial will start, made local now, so no trial stalls on a first pull or build: graders, MCP
    servers (pulled unless setup() built them) and the agent's own derived images (e.g. Claude Code's)."""
    for image in sorted({benchmark.grader_image(t) for t in tasks} | {s.image for t in tasks for s in benchmark.mcp_servers(t)}):
        ensure_image(image)
    for t in tasks:
        agent.prepare_image(t.image)


def _attempt(store: Store, db_path: Path, worker: str, claim: Claim, stop: Stop) -> None:
    """One attempt at a trial; then, in one transaction, its outcome and the trial's next state (done, or re-queued)."""
    run = store.run(claim.run_id)
    benchmark = BENCHMARKS[run["benchmark"]]()
    agent = resolve_agent(run["agent"])(run["model"])
    task = next(t for t in benchmark.tasks() if t.id == claim.task_id)
    limits = Limits(**json.loads(run["config"])["limits"])

    def turn_ended(stats: AgentStats) -> None:  # live spend and trace; a failure here only delays them to the attempt's end
        try:
            store.record_spend(claim.attempt_id, stats)
            store.load_events(claim.attempt_id, claim.artifacts_dir / "events.jsonl")
        except sqlite3.Error as e:
            log.warning("%s attempt %s: recording the turn failed (%s); retried at the end", claim.task_id, claim.n, e)

    def environment(image: str, lifetime_s: int, limits: Limits, workdir: str) -> Environment:  # ENVIRONMENT, known to Stop
        if stop.requested.is_set():
            raise EnvironmentLost("worker interrupted")
        env = ENVIRONMENT(image, lifetime_s, limits, workdir, labels={ATTEMPT_LABEL: str(claim.attempt_id)})  # for `harness cleanup`
        stop.track_world(claim, env)
        return env

    log.info("run %s: %s attempt %s", claim.run_id, claim.task_id, claim.n)
    with _heartbeat(db_path, lambda s: s.renew_lease(claim, worker, LEASE_S)):
        outcome = run_trial(benchmark, agent, task, limits, claim.artifacts_dir, environment, on_turn_end=turn_ended)
    # first, and whatever happens next (interrupted, claim lost): money spent counts toward the run's budget
    store.record_spend(claim.attempt_id, AgentStats(None, outcome.cost_usd, outcome.model_calls, outcome.input_tokens,
                                                    outcome.output_tokens))
    if stop.requested.is_set():  # Ctrl-C also reaches our `docker exec`s: whatever this attempt ended as, it's not its own
        _interrupted(store, claim, worker)
        return
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
