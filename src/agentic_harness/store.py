"""The database: runs, trials (which are also the queue), attempts, events.

Plain sqlite3 with handwritten SQL. One Store = one connection; every thread opens its own Store.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from agentic_harness.types import Outcome

SCHEMA_VERSION = 3  # bump on any schema change; an old harness.db is then refused, not migrated

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    benchmark TEXT NOT NULL,
    agent TEXT NOT NULL,
    model TEXT,
    versions TEXT NOT NULL,     -- json: harness commit, agent commit, dataset hash, model
    config TEXT NOT NULL,       -- json: limits
    status TEXT NOT NULL,       -- pending (waiting for setup) | running | done | failed (setup failed)
    setup_claimed_by TEXT,
    setup_lease_until REAL,     -- the setup claim lapses after this (time.time()) unless renewed
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs (id),
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,        -- pending | running | done
    claimed_by TEXT,
    lease_until REAL,           -- the claim lapses after this (time.time()) unless renewed
    available_at REAL NOT NULL DEFAULT 0,  -- not claimable before this (retry backoff)
    attempt_count INTEGER NOT NULL DEFAULT 0,  -- attempts started; the current one is n = attempt_count
    outcome TEXT,               -- graded | grader_error | infra_error | unsupported
    score_value TEXT,           -- json: the grader's raw verdict
    score_float REAL,           -- what accuracy averages
    UNIQUE (run_id, task_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    trial_id INTEGER NOT NULL REFERENCES trials (id),
    n INTEGER NOT NULL,
    outcome TEXT,               -- NULL while the attempt runs
    score TEXT,                 -- json Score
    exit_status TEXT,
    error TEXT,
    cost_usd REAL,
    duration_s REAL,
    artifacts_dir TEXT,
    started_at REAL,            -- claimed (time.time()); NULL for a setup failure's attempt
    finished_at REAL,
    UNIQUE (trial_id, n)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    attempt_id INTEGER NOT NULL REFERENCES attempts (id),
    seq INTEGER NOT NULL,
    turn INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,      -- json
    ts REAL NOT NULL,
    UNIQUE (attempt_id, seq)    -- loading the same events.jsonl again adds nothing
);
CREATE TABLE IF NOT EXISTS workers (  -- one row per running `harness worker` process, so status can say none is
    id TEXT PRIMARY KEY,        -- host-pid
    started_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL  -- renewed every 30 s; a stale row is a worker that died without saying goodbye
);
"""


class SchemaMismatch(Exception):
    """harness.db was made by a harness with another schema."""


class LostClaim(Exception):
    """This worker no longer holds the trial or setup (its lease expired and another worker reclaimed it)."""


@dataclass(frozen=True)
class Claim:
    """A trial held by one worker, and the attempt it is making (created with the claim)."""

    trial_id: int
    run_id: str
    task_id: str
    attempt_id: int
    n: int  # 1, 2, 3: which attempt at this trial
    artifacts_dir: Path  # runs/<run_id>/<task_id>/attempt-<attempt_id>/: never shared by two attempts


class Store:
    def __init__(self, path: Path) -> None:
        self.runs_dir = path.parent  # attempt folders live next to the DB
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit; transactions are explicit (_transaction)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")  # a writer waits up to 30 s for the lock instead of failing
        self.db.execute("PRAGMA journal_mode=WAL")  # readers (status) never wait for the writer
        self.db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        self.db.execute("INSERT INTO schema_version SELECT ? WHERE NOT EXISTS (SELECT 1 FROM schema_version)", (SCHEMA_VERSION,))
        version = self.db.execute("SELECT version FROM schema_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise SchemaMismatch(f"{path} has schema version {version}, this harness needs {SCHEMA_VERSION}: delete {path}")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")  # take the write lock now: nothing read inside can change before we write
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.db.execute(sql, params).fetchall()  # fetch everything, so the statement is finished before COMMIT
        return rows[0] if rows else None

    # --- submitting ---

    def create_run(self, run_id: str, benchmark: str, agent: str, model: str | None, versions: dict, config: dict,
                   task_ids: list[str]) -> None:
        with self._transaction():
            self.db.execute(
                "INSERT INTO runs (id, benchmark, agent, model, versions, config, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (run_id, benchmark, agent, model, json.dumps(versions), json.dumps(config), time.time()),
            )
            self.db.executemany(
                "INSERT INTO trials (run_id, task_id, state) VALUES (?, ?, 'pending')", [(run_id, t) for t in task_ids]
            )

    # --- run setup (once per run, by the worker that claims it) ---

    def claim_setup(self, worker: str, lease_s: float, run_id: str | None = None) -> sqlite3.Row | None:
        """The oldest run waiting for setup (unclaimed, or its setup worker's lease expired), now claimed by `worker`."""
        now = time.time()
        with self._transaction():
            return self._one(
                """UPDATE runs SET setup_claimed_by = ?, setup_lease_until = ?
                   WHERE id = (SELECT id FROM runs
                               WHERE status = 'pending' AND (setup_claimed_by IS NULL OR setup_lease_until < ?)
                                 AND (? IS NULL OR id = ?)
                               ORDER BY created_at LIMIT 1)
                   RETURNING *""",
                (worker, now + lease_s, now, run_id, run_id),
            )

    def renew_setup_lease(self, run_id: str, worker: str, lease_s: float) -> bool:
        """Extend the setup claim; False if this worker no longer holds it."""
        cursor = self.db.execute(
            "UPDATE runs SET setup_lease_until = ? WHERE id = ? AND setup_claimed_by = ? AND status = 'pending'",
            (time.time() + lease_s, run_id, worker),
        )
        return cursor.rowcount == 1

    def finish_setup(self, run_id: str, worker: str, error: str | None = None) -> None:
        """Setup worked: the run's trials become claimable. Setup failed: the run fails and every trial ends infra_error.
        Raises LostClaim (and writes nothing) if `worker` no longer holds the setup."""
        with self._transaction():
            cursor = self.db.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE id = ? AND setup_claimed_by = ? AND status = 'pending'",
                ("running" if error is None else "failed", None if error is None else time.time(), run_id, worker),
            )
            if cursor.rowcount == 0:
                raise LostClaim(f"setup of {run_id}")
            if error is not None:
                self.db.execute(  # one attempt row per trial, so the error is where every other error is
                    "INSERT INTO attempts (trial_id, n, outcome, error) SELECT id, 1, 'infra_error', ? FROM trials WHERE run_id = ?",
                    (f"setup failed: {error}", run_id),
                )
                self.db.execute(
                    "UPDATE trials SET state = 'done', outcome = 'infra_error', attempt_count = 1 WHERE run_id = ?", (run_id,)
                )

    # --- the queue ---

    def claim_trial(self, worker: str, lease_s: float, run_id: str | None = None) -> Claim | None:
        """Take the next pending trial of a run whose setup is done, for lease_s, and start its next attempt.
        None if nothing is claimable."""
        now = time.time()
        with self._transaction():
            trial = self._one(
                """UPDATE trials SET state = 'running', claimed_by = ?, lease_until = ?, attempt_count = attempt_count + 1
                   WHERE id = (SELECT t.id FROM trials t JOIN runs r ON r.id = t.run_id
                               WHERE t.state = 'pending' AND t.available_at <= ? AND r.status = 'running'
                                 AND (? IS NULL OR r.id = ?)
                               ORDER BY r.created_at, t.id LIMIT 1)
                   RETURNING id, run_id, task_id, attempt_count""",
                (worker, now + lease_s, now, run_id, run_id),
            )
            if trial is None:
                return None
            attempt_id = self.db.execute(
                "INSERT INTO attempts (trial_id, n, started_at) VALUES (?, ?, ?)", (trial["id"], trial["attempt_count"], now)
            ).lastrowid
            artifacts_dir = self.runs_dir / trial["run_id"] / trial["task_id"] / f"attempt-{attempt_id}"
            self.db.execute("UPDATE attempts SET artifacts_dir = ? WHERE id = ?", (str(artifacts_dir), attempt_id))
        return Claim(trial["id"], trial["run_id"], trial["task_id"], attempt_id, trial["attempt_count"], artifacts_dir)

    def renew_lease(self, claim: Claim, worker: str, lease_s: float) -> bool:
        """Extend the claim; False if this worker no longer holds it."""
        cursor = self.db.execute(
            "UPDATE trials SET lease_until = ? WHERE id = ? AND claimed_by = ? AND state = 'running' AND attempt_count = ?",
            (time.time() + lease_s, claim.trial_id, worker, claim.n),
        )
        return cursor.rowcount == 1

    def reclaim_expired(self, max_attempts: int, backoff_s: float) -> list[sqlite3.Row]:
        """Trials whose worker stopped renewing its lease (killed, hung): close the open attempt as infra_error
        ("worker lost"; it counts toward max_attempts), then re-queue the trial after a backoff, or end it
        infra_error once it has had max_attempts (so a trial that kills its worker can't loop forever)."""
        now = time.time()
        with self._transaction():
            expired = self.db.execute(
                "SELECT id, run_id, attempt_count FROM trials WHERE state = 'running' AND lease_until < ?", (now,)
            ).fetchall()
            for t in expired:
                self.db.execute(
                    "UPDATE attempts SET outcome = 'infra_error', error = 'worker lost: lease expired', finished_at = ?"
                    " WHERE trial_id = ? AND n = ? AND outcome IS NULL",
                    (now, t["id"], t["attempt_count"]),
                )
                self._end_or_requeue(t["id"], t["run_id"], Outcome("infra_error"),
                                     None if t["attempt_count"] >= max_attempts else now + backoff_s * t["attempt_count"])
        return expired

    def finish_attempt(self, claim: Claim, worker: str, outcome: Outcome, retry_at: float | None = None) -> None:
        """The commit point of an attempt (its files are already written): record its outcome, load the rest of its
        events, and end the trial with it, or (retry_at given) re-queue the trial, claimable from retry_at.
        Raises LostClaim (and writes nothing) if `worker` no longer holds this attempt's claim."""
        with self._transaction():
            owned = self._one(
                "SELECT 1 FROM trials WHERE id = ? AND claimed_by = ? AND state = 'running' AND attempt_count = ?",
                (claim.trial_id, worker, claim.n),
            )
            if owned is None:
                raise LostClaim(f"trial {claim.trial_id} attempt {claim.n}")
            self._load_events(claim.attempt_id, claim.artifacts_dir / "events.jsonl")
            self.db.execute(
                "UPDATE attempts SET outcome = ?, score = ?, exit_status = ?, error = ?, cost_usd = ?, duration_s = ?,"
                " finished_at = ? WHERE id = ?",
                (outcome.status, _json(outcome.score and asdict(outcome.score)), outcome.agent_exit_status, outcome.error,
                 outcome.cost_usd, outcome.duration_s, time.time(), claim.attempt_id),
            )
            self._end_or_requeue(claim.trial_id, claim.run_id, outcome, retry_at)

    def _end_or_requeue(self, trial_id: int, run_id: str, outcome: Outcome, retry_at: float | None) -> None:
        if retry_at is not None:
            self.db.execute(
                "UPDATE trials SET state = 'pending', claimed_by = NULL, lease_until = NULL, available_at = ? WHERE id = ?",
                (retry_at, trial_id),
            )
            return
        self.db.execute(  # done is final: the claim query only takes pending trials
            "UPDATE trials SET state = 'done', outcome = ?, score_value = ?, score_float = ?, lease_until = NULL WHERE id = ?",
            (outcome.status, _json(outcome.score and outcome.score.value), outcome.score_float, trial_id),
        )
        self.db.execute(
            "UPDATE runs SET status = 'done', finished_at = ? WHERE id = ? AND status = 'running'"
            " AND NOT EXISTS (SELECT 1 FROM trials WHERE run_id = ? AND state != 'done')",
            (time.time(), run_id, run_id),
        )

    def load_events(self, attempt_id: int, events_file: Path) -> None:
        """Copy the attempt's events.jsonl into the events table (lines already loaded are skipped)."""
        with self._transaction():
            self._load_events(attempt_id, events_file)

    def _load_events(self, attempt_id: int, events_file: Path) -> None:
        if not events_file.exists():
            return
        events = [json.loads(line) for line in events_file.read_text().splitlines() if line]
        self.db.executemany(
            "INSERT OR IGNORE INTO events (attempt_id, seq, turn, kind, payload, ts) VALUES (?, ?, ?, ?, ?, ?)",
            [(attempt_id, e["seq"], e["turn"], e["kind"], json.dumps(e["payload"]), e["ts"]) for e in events],
        )

    # --- workers ---

    def worker_started(self, worker: str) -> None:
        now = time.time()
        self.db.execute("INSERT OR REPLACE INTO workers (id, started_at, heartbeat_at) VALUES (?, ?, ?)", (worker, now, now))

    def worker_heartbeat(self, worker: str) -> bool:
        """Always True: a worker that lost its row (e.g. pruned) simply adds it back."""
        self.db.execute(
            "INSERT INTO workers (id, started_at, heartbeat_at) VALUES (?, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at",
            (worker, time.time(), time.time()),
        )
        return True

    def worker_stopped(self, worker: str) -> None:
        self.db.execute("DELETE FROM workers WHERE id = ?", (worker,))

    def live_workers(self, since: float) -> int:
        """Workers whose last heartbeat is at or after `since`."""
        return self._one("SELECT COUNT(*) FROM workers WHERE heartbeat_at >= ?", (since,))[0]

    # --- reading back (status, results) ---

    def run(self, run_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def runs(self, finished_since: float | None = None) -> list[sqlite3.Row]:
        """Runs with their trial counts: all of them, or (finished_since given) the active ones plus those finished since."""
        return self.db.execute(
            """SELECT r.*, COUNT(t.id) AS total, COALESCE(SUM(t.state = 'done'), 0) AS done
               FROM runs r LEFT JOIN trials t ON t.run_id = r.id
               WHERE ? IS NULL OR r.status IN ('pending', 'running') OR r.finished_at >= ?
               GROUP BY r.id ORDER BY r.created_at""",
            (finished_since, finished_since),
        ).fetchall()

    def trials(self, run_id: str) -> list[sqlite3.Row]:
        """The run's trials, each with its cost and time summed over its attempts."""
        return self.db.execute(
            """SELECT t.*, COALESCE(SUM(a.cost_usd), 0) AS cost_usd, COALESCE(SUM(a.duration_s), 0) AS duration_s
               FROM trials t LEFT JOIN attempts a ON a.trial_id = t.id
               WHERE t.run_id = ? GROUP BY t.id ORDER BY t.id""",
            (run_id,),
        ).fetchall()

    def attempts(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT a.* FROM attempts a JOIN trials t ON t.id = a.trial_id WHERE t.run_id = ? ORDER BY a.trial_id, a.n",
            (run_id,),
        ).fetchall()

    def accuracy(self, run_id: str) -> tuple[float | None, int]:
        """Mean score_float over graded trials only, and how many there are."""
        row = self._one("SELECT AVG(score_float), COUNT(*) FROM trials WHERE run_id = ? AND outcome = 'graded'", (run_id,))
        return row[0], row[1]

    def trial_span(self, run_id: str) -> tuple[float | None, float | None]:
        """When the run's first attempt started and its last finished attempt ended (None if none has)."""
        row = self._one(
            "SELECT MIN(a.started_at), MAX(a.finished_at) FROM attempts a JOIN trials t ON t.id = a.trial_id WHERE t.run_id = ?",
            (run_id,),
        )
        return row[0], row[1]

    def unfinished_runs(self, run_id: str | None = None) -> list[sqlite3.Row]:
        """Runs (or this run) still waiting for setup or running."""
        return self.db.execute(
            "SELECT * FROM runs WHERE status IN ('pending', 'running') AND (? IS NULL OR id = ?) ORDER BY created_at",
            (run_id, run_id),
        ).fetchall()

    def waiting_trials(self) -> int:
        """Trials a worker still has to finish: not done, in runs that are not over (pending setup or running)."""
        return self._one(
            "SELECT COUNT(*) FROM trials t JOIN runs r ON r.id = t.run_id"
            " WHERE t.state != 'done' AND r.status IN ('pending', 'running')"
        )[0]

    def unfinished(self, run_id: str | None = None) -> bool:
        """Is any run (or this one) still waiting for setup or running?"""
        return self._one(
            "SELECT 1 FROM runs WHERE status IN ('pending', 'running') AND (? IS NULL OR id = ?) LIMIT 1", (run_id, run_id)
        ) is not None


def _json(value) -> str | None:
    return None if value is None else json.dumps(value)
