import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from agentic_harness.agents.base import AgentStats
from agentic_harness.redact import redact
from agentic_harness.types import Outcome

SCHEMA_VERSION = 5  # bump on any schema change; an old harness.db is then refused

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    benchmark TEXT NOT NULL,
    agent TEXT NOT NULL,
    model TEXT,
    versions TEXT NOT NULL,     -- json: harness commit, agent commit, dataset hash, model
    config TEXT NOT NULL,       -- json: default limits, the limit flags given, which of them make the run non-standard
    max_cost_usd REAL,          -- the run's budget: once its attempts have spent this, no more trials start
    status TEXT NOT NULL,       -- pending (waiting for setup) | running | done | failed (setup failed)
    setup_claimed_by TEXT,
    setup_lease_until REAL,     -- the setup claim lapses after this (time.time()) unless renewed
    setup_started_at REAL,      -- the latest setup claim
    setup_finished_at REAL,     -- the trials became claimable (or the run failed)
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
    outcome TEXT,               -- graded | grader_error | infra_error | unsupported | skipped_budget (never started)
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
    cost_usd REAL,              -- what the agent spent; recorded at each turn's end, so also for a crashed attempt
    model_calls INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    duration_s REAL,
    artifacts_dir TEXT,
    limits TEXT,                -- json: the limits it ran under, each with its source (default | benchmark | flag)
    -- phase boundaries (time.time()); NULL for a phase the attempt never reached, and for a setup failure's attempt
    queued_at REAL,             -- its trial became claimable (setup done, or a retry's backoff over)
    started_at REAL,            -- claimed
    env_ready_at REAL,          -- world and sidecars up
    agent_start_at REAL,
    agent_end_at REAL,
    grade_start_at REAL,
    grade_end_at REAL,
    finished_at REAL,           -- outcome recorded
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
    ...


class LostClaim(Exception):
    ...


@dataclass(frozen=True)
class Claim:
    trial_id: int
    run_id: str
    task_id: str
    attempt_id: int
    n: int
    artifacts_dir: Path


class Store:
    def __init__(self, path: Path) -> None:
        self.runs_dir = path.parent
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit; transactions are explicit (_transaction)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
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

    def create_run(self, run_id: str, benchmark: str, agent: str, model: str | None, versions: dict, config: dict,
                   task_ids: list[str], max_cost_usd: float | None = None) -> None:
        with self._transaction():
            self.db.execute(
                "INSERT INTO runs (id, benchmark, agent, model, versions, config, max_cost_usd, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (run_id, benchmark, agent, model, json.dumps(versions), json.dumps(config), max_cost_usd, time.time()),
            )
            self.db.executemany(
                "INSERT INTO trials (run_id, task_id, state) VALUES (?, ?, 'pending')", [(run_id, t) for t in task_ids]
            )

    def claim_setup(self, worker: str, lease_s: float, run_id: str | None = None) -> sqlite3.Row | None:
        now = time.time()
        with self._transaction():
            return self._one(
                """UPDATE runs SET setup_claimed_by = ?, setup_lease_until = ?, setup_started_at = ?
                   WHERE id = (SELECT id FROM runs
                               WHERE status = 'pending' AND (setup_claimed_by IS NULL OR setup_lease_until < ?)
                                 AND (? IS NULL OR id = ?)
                               ORDER BY created_at LIMIT 1)
                   RETURNING *""",
                (worker, now + lease_s, now, now, run_id, run_id),
            )

    def renew_setup_lease(self, run_id: str, worker: str, lease_s: float) -> bool:
        cursor = self.db.execute(
            "UPDATE runs SET setup_lease_until = ? WHERE id = ? AND setup_claimed_by = ? AND status = 'pending'",
            (time.time() + lease_s, run_id, worker),
        )
        return cursor.rowcount == 1

    def release_setup(self, run_id: str, worker: str) -> None:
        self.db.execute(
            "UPDATE runs SET setup_claimed_by = NULL, setup_lease_until = NULL"
            " WHERE id = ? AND setup_claimed_by = ? AND status = 'pending'",
            (run_id, worker),
        )

    def finish_setup(self, run_id: str, worker: str, error: str | None = None) -> None:
        with self._transaction():
            now = time.time()
            cursor = self.db.execute(
                "UPDATE runs SET status = ?, setup_finished_at = ?, finished_at = ?"
                " WHERE id = ? AND setup_claimed_by = ? AND status = 'pending'",
                ("running" if error is None else "failed", now, None if error is None else now, run_id, worker),
            )
            if cursor.rowcount == 0:
                raise LostClaim(f"setup of {run_id}")
            if error is not None:
                self.db.execute(
                    "INSERT INTO attempts (trial_id, n, outcome, error) SELECT id, 1, 'infra_error', ? FROM trials WHERE run_id = ?",
                    (f"setup failed: {error}", run_id),
                )
                self.db.execute(
                    "UPDATE trials SET state = 'done', outcome = 'infra_error', attempt_count = 1 WHERE run_id = ?", (run_id,)
                )

    def fail_run(self, run_id: str, error: str) -> bool:
        with self._transaction():
            now = time.time()
            cursor = self.db.execute(
                "UPDATE runs SET status = 'failed', finished_at = ? WHERE id = ? AND status IN ('pending', 'running')",
                (now, run_id),
            )
            if cursor.rowcount == 0:
                return False
            self.db.execute(
                "UPDATE attempts SET outcome = 'infra_error', error = ?, finished_at = ? WHERE outcome IS NULL"
                " AND trial_id IN (SELECT id FROM trials WHERE run_id = ? AND state = 'running')",
                (error, now, run_id),
            )
            self.db.execute(
                "INSERT INTO attempts (trial_id, n, outcome, error, finished_at)"
                " SELECT id, attempt_count + 1, 'infra_error', ?, ? FROM trials WHERE run_id = ? AND state = 'pending'",
                (error, now, run_id),
            )
            self.db.execute(
                "UPDATE trials SET attempt_count = attempt_count + (state = 'pending'), state = 'done',"
                " outcome = 'infra_error', claimed_by = NULL, lease_until = NULL WHERE run_id = ? AND state != 'done'",
                (run_id,),
            )
        return True

    def claim_trial(self, worker: str, lease_s: float, run_id: str | None = None,
                    max_running: int | None = None) -> Claim | None:
        now = time.time()
        with self._transaction():  # the count and the claim in one write transaction: two workers can't both pass
            trial = self._one(
                """UPDATE trials SET state = 'running', claimed_by = ?, lease_until = ?, attempt_count = attempt_count + 1
                   WHERE id = (SELECT t.id FROM trials t JOIN runs r ON r.id = t.run_id
                               WHERE t.state = 'pending' AND t.available_at <= ? AND r.status = 'running'
                                 AND (? IS NULL OR r.id = ?)
                                 AND (r.max_cost_usd IS NULL OR (SELECT COALESCE(SUM(a.cost_usd), 0) FROM attempts a
                                       JOIN trials t2 ON t2.id = a.trial_id WHERE t2.run_id = r.id) < r.max_cost_usd)
                                 AND (? IS NULL OR (SELECT COUNT(*) FROM trials WHERE state = 'running'
                                                    AND lease_until >= ?) < ?)
                               ORDER BY r.created_at, t.id LIMIT 1)
                   RETURNING id, run_id, task_id, attempt_count, available_at""",
                (worker, now + lease_s, now, run_id, run_id, max_running, now, max_running),
            )
            if trial is None:
                return None
            attempt_id = self.db.execute(
                "INSERT INTO attempts (trial_id, n, queued_at, started_at)"
                " SELECT ?, ?, MAX(COALESCE(setup_finished_at, created_at), ?), ? FROM runs WHERE id = ?",
                (trial["id"], trial["attempt_count"], trial["available_at"], now, trial["run_id"]),
            ).lastrowid
            artifacts_dir = self.runs_dir / trial["run_id"] / trial["task_id"] / f"attempt-{attempt_id}"
            self.db.execute("UPDATE attempts SET artifacts_dir = ? WHERE id = ?", (str(artifacts_dir), attempt_id))
        return Claim(trial["id"], trial["run_id"], trial["task_id"], attempt_id, trial["attempt_count"], artifacts_dir)

    def renew_lease(self, claim: Claim, worker: str, lease_s: float) -> bool:
        cursor = self.db.execute(
            "UPDATE trials SET lease_until = ? WHERE id = ? AND claimed_by = ? AND state = 'running' AND attempt_count = ?",
            (time.time() + lease_s, claim.trial_id, worker, claim.n),
        )
        return cursor.rowcount == 1

    def reclaim_expired(self, max_attempts: int, backoff_s: float) -> list[sqlite3.Row]:
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
        """The attempt's commit point; raises LostClaim (writing nothing) if not ours."""
        with self._transaction():
            owned = self._one(
                "SELECT 1 FROM trials WHERE id = ? AND claimed_by = ? AND state = 'running' AND attempt_count = ?",
                (claim.trial_id, worker, claim.n),
            )
            if owned is None:
                raise LostClaim(f"trial {claim.trial_id} attempt {claim.n}")
            self._load_events(claim.attempt_id, claim.artifacts_dir / "events.jsonl")
            phases = outcome.phases
            self.db.execute(
                "UPDATE attempts SET outcome = ?, score = ?, exit_status = ?, error = ?, duration_s = ?, limits = ?,"
                " env_ready_at = ?, agent_start_at = ?, agent_end_at = ?, grade_start_at = ?, grade_end_at = ?,"
                " finished_at = ? WHERE id = ?",
                (outcome.status, _json(outcome.score and asdict(outcome.score)), outcome.agent_exit_status and redact(outcome.agent_exit_status),
                 outcome.error and redact(outcome.error),
                 outcome.duration_s, _json(outcome.limits or None), phases.get("env_ready"), phases.get("agent_start"),
                 phases.get("agent_end"), phases.get("grade_start"), phases.get("grade_end"), time.time(), claim.attempt_id),
            )
            self._record_spend(claim.attempt_id, outcome.cost_usd, outcome.model_calls, outcome.input_tokens,
                               outcome.output_tokens)
            self._end_or_requeue(claim.trial_id, claim.run_id, outcome, retry_at)

    def record_spend(self, attempt_id: int, stats: AgentStats) -> None:
        """Not fenced: money a worker spent before losing its claim was still spent."""
        with self._transaction():
            self._record_spend(attempt_id, stats.cost_usd, stats.model_calls, stats.input_tokens, stats.output_tokens)
            run_id = self._one("SELECT t.run_id FROM attempts a JOIN trials t ON t.id = a.trial_id WHERE a.id = ?",
                               (attempt_id,))[0]
            self._apply_budget(run_id)

    def _record_spend(self, attempt_id: int, cost_usd: float, model_calls: int, input_tokens: int, output_tokens: int) -> None:
        # MAX: spend only grows; an outcome without stats never erases it
        self.db.execute(
            "UPDATE attempts SET cost_usd = MAX(COALESCE(cost_usd, 0), ?), model_calls = MAX(COALESCE(model_calls, 0), ?),"
            " input_tokens = MAX(COALESCE(input_tokens, 0), ?), output_tokens = MAX(COALESCE(output_tokens, 0), ?)"
            " WHERE id = ?",
            (cost_usd, model_calls, input_tokens, output_tokens, attempt_id),
        )

    def _end_or_requeue(self, trial_id: int, run_id: str, outcome: Outcome, retry_at: float | None) -> None:
        if retry_at is not None:
            self.db.execute(
                "UPDATE trials SET state = 'pending', claimed_by = NULL, lease_until = NULL, available_at = ? WHERE id = ?",
                (retry_at, trial_id),
            )
        else:
            self.db.execute(
                "UPDATE trials SET state = 'done', outcome = ?, score_value = ?, score_float = ?, lease_until = NULL"
                " WHERE id = ?",
                (outcome.status, _json(outcome.score and outcome.score.value), outcome.score_float, trial_id),
            )
        self._apply_budget(run_id)

    def _apply_budget(self, run_id: str) -> None:
        self.db.execute(
            "UPDATE trials SET state = 'done', outcome = 'skipped_budget' WHERE run_id = ? AND state = 'pending'"
            " AND (SELECT max_cost_usd FROM runs WHERE id = ?) <= (SELECT COALESCE(SUM(a.cost_usd), 0) FROM attempts a"
            " JOIN trials t ON t.id = a.trial_id WHERE t.run_id = ?)",
            (run_id, run_id, run_id),
        )
        self.db.execute(
            "UPDATE runs SET status = 'done', finished_at = ? WHERE id = ? AND status = 'running'"
            " AND NOT EXISTS (SELECT 1 FROM trials WHERE run_id = ? AND state != 'done')",
            (time.time(), run_id, run_id),
        )

    def load_events(self, attempt_id: int, events_file: Path) -> None:
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

    def worker_started(self, worker: str) -> None:
        now = time.time()
        self.db.execute("INSERT OR REPLACE INTO workers (id, started_at, heartbeat_at) VALUES (?, ?, ?)", (worker, now, now))

    def worker_heartbeat(self, worker: str) -> bool:
        self.db.execute(
            "INSERT INTO workers (id, started_at, heartbeat_at) VALUES (?, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at",
            (worker, time.time(), time.time()),
        )
        return True

    def worker_stopped(self, worker: str) -> None:
        self.db.execute("DELETE FROM workers WHERE id = ?", (worker,))

    def live_workers(self, since: float) -> int:
        return self._one("SELECT COUNT(*) FROM workers WHERE heartbeat_at >= ?", (since,))[0]

    def run(self, run_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def runs(self, finished_since: float | None = None) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT r.*, COUNT(t.id) AS total, COALESCE(SUM(t.state = 'done'), 0) AS done,
                      COALESCE(SUM(t.outcome = 'skipped_budget'), 0) AS skipped,
                      (SELECT COALESCE(SUM(a.cost_usd), 0) FROM attempts a JOIN trials t2 ON t2.id = a.trial_id
                       WHERE t2.run_id = r.id) AS cost_usd
               FROM runs r LEFT JOIN trials t ON t.run_id = r.id
               WHERE ? IS NULL OR r.status IN ('pending', 'running') OR r.finished_at >= ?
               GROUP BY r.id ORDER BY r.created_at""",
            (finished_since, finished_since),
        ).fetchall()

    def trials(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT t.*, COALESCE(SUM(a.cost_usd), 0) AS cost_usd, COALESCE(SUM(a.duration_s), 0) AS duration_s,
                      COALESCE(SUM(a.input_tokens), 0) AS input_tokens, COALESCE(SUM(a.output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(a.model_calls), 0) AS model_calls,
                      COALESCE(SUM(a.agent_end_at - a.agent_start_at), 0) AS agent_s,
                      COALESCE(SUM(a.grade_end_at - a.grade_start_at), 0) AS grading_s,
                      MAX(CASE WHEN a.outcome IS NULL THEN a.started_at END) AS running_since
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
        row = self._one("SELECT AVG(score_float), COUNT(*) FROM trials WHERE run_id = ? AND outcome = 'graded'", (run_id,))
        return row[0], row[1]

    def unfinished_runs(self, run_id: str | None = None) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM runs WHERE status IN ('pending', 'running') AND (? IS NULL OR id = ?) ORDER BY created_at",
            (run_id, run_id),
        ).fetchall()

    def live_attempts(self) -> set[str]:
        rows = self.db.execute(
            "SELECT a.id FROM attempts a JOIN trials t ON t.id = a.trial_id"
            " WHERE t.state = 'running' AND t.lease_until >= ? AND a.n = t.attempt_count AND a.outcome IS NULL",
            (time.time(),),
        ).fetchall()
        return {str(r[0]) for r in rows}

    def waiting_trials(self) -> int:
        return self._one(
            "SELECT COUNT(*) FROM trials t JOIN runs r ON r.id = t.run_id"
            " WHERE t.state != 'done' AND r.status IN ('pending', 'running')"
        )[0]

    def unfinished(self, run_id: str | None = None) -> bool:
        return self._one(
            "SELECT 1 FROM runs WHERE status IN ('pending', 'running') AND (? IS NULL OR id = ?) LIMIT 1", (run_id, run_id)
        ) is not None


def _json(value) -> str | None:
    return None if value is None else redact(json.dumps(value))
