"""The dashboard: a read-only local web view of runs/harness.db and the attempt folders.

Standard library only. The server never writes: SQLite is opened read-only, and the only files it serves are the
dashboard's own static files and the files inside an attempt folder under runs/.
"""

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentic_harness.metrics import run_metrics
from agentic_harness.paths import REPO_ROOT
from agentic_harness.store import SCHEMA_VERSION, SchemaMismatch

STATIC_DIR = REPO_ROOT / "dashboard"
STATIC_FILES = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
                 ".jsonl": "text/plain", ".log": "text/plain", ".tar": "application/x-tar"}
WORKER_ALIVE_S = 120  # a worker heartbeats every 30 s; silent for a whole lease (worker.LEASE_S) = gone

RUN_ID = re.compile(r"[\w.-]+")
FILE_NAME = re.compile(r"\w[\w.-]*")  # one name inside an attempt folder: no slashes, no leading dot

RUNS_SQL = """
WITH spent AS (
    SELECT t.run_id, SUM(a.cost_usd) AS cost_usd, SUM(a.duration_s) AS trial_time_s,
           MIN(a.started_at) AS first_started_at, MAX(a.finished_at) AS last_finished_at
    FROM attempts a JOIN trials t ON t.id = a.trial_id GROUP BY t.run_id
)
SELECT r.id, r.benchmark, r.agent, r.model, r.status, r.setup_claimed_by IS NOT NULL AS setup_claimed, r.max_cost_usd,
       r.created_at, r.finished_at, s.first_started_at, s.last_finished_at,
       COALESCE(s.cost_usd, 0) AS cost_usd, COALESCE(s.trial_time_s, 0) AS trial_time_s,
       COUNT(t.id) AS total,
       COALESCE(SUM(t.state = 'done'), 0) AS done,
       COALESCE(SUM(t.state = 'running'), 0) AS running,
       COALESCE(SUM(t.outcome = 'graded'), 0) AS graded,
       COALESCE(SUM(t.outcome = 'graded' AND t.score_float >= 1), 0) AS passed,
       COALESCE(SUM(t.outcome = 'grader_error'), 0) AS grader_error,
       COALESCE(SUM(t.outcome = 'infra_error'), 0) AS infra_error,
       COALESCE(SUM(t.outcome = 'unsupported'), 0) AS unsupported,
       COALESCE(SUM(t.outcome = 'skipped_budget'), 0) AS skipped_budget,
       AVG(CASE WHEN t.outcome = 'graded' THEN t.score_float END) AS accuracy
FROM runs r LEFT JOIN trials t ON t.run_id = r.id LEFT JOIN spent s ON s.run_id = r.id
WHERE ? IS NULL OR r.id = ?
GROUP BY r.id
ORDER BY r.status IN ('pending', 'running') DESC, r.created_at DESC
"""

TRIALS_SQL = """
SELECT t.id, t.task_id, t.state, t.outcome, t.score_value, t.score_float, t.attempt_count,
       COALESCE(SUM(a.cost_usd), 0) AS cost_usd, COALESCE(SUM(a.duration_s), 0) AS duration_s,
       COALESCE(SUM(a.input_tokens), 0) AS input_tokens, COALESCE(SUM(a.output_tokens), 0) AS output_tokens,
       SUM(a.agent_end_at - a.agent_start_at) AS agent_s, SUM(a.grade_end_at - a.grade_start_at) AS grading_s,
       json_group_array(json_object('id', a.id, 'n', a.n, 'outcome', a.outcome)) FILTER (WHERE a.id IS NOT NULL) AS attempts
FROM trials t LEFT JOIN attempts a ON a.trial_id = t.id
WHERE t.run_id = ? GROUP BY t.id ORDER BY t.id
"""

ATTEMPT_SQL = """
SELECT a.*, t.task_id, t.run_id FROM attempts a JOIN trials t ON t.id = a.trial_id WHERE a.id = ?
"""


class NotFound(Exception):
    pass


class Dashboard:
    """The data behind the endpoints: one read-only connection per request (the server is threaded)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.runs_dir = db_path.parent.resolve()

    @contextmanager
    def _db(self):
        if not self.db_path.exists():  # nothing submitted yet; mode=ro fails rather than creating the file
            raise NotFound("no database yet")
        db = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        version = db.execute("SELECT version FROM schema_version").fetchone()[0]
        if version != SCHEMA_VERSION:  # the queries below would fail on missing columns; say why instead
            db.close()
            raise SchemaMismatch(f"{self.db_path} has schema version {version}, this harness needs {SCHEMA_VERSION}: "
                                 f"delete it (or check out the harness that made it)")
        try:
            yield db
        finally:
            db.close()

    def runs(self, run_id: str | None = None) -> list[dict]:
        if not self.db_path.exists():
            return []
        with self._db() as db:
            return [dict(r) for r in db.execute(RUNS_SQL, (run_id, run_id))]

    def run(self, run_id: str) -> dict:
        found = self.runs(run_id) if RUN_ID.fullmatch(run_id) else []
        if not found:
            raise NotFound(f"no run {run_id}")
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            trials = db.execute(TRIALS_SQL, (run_id,)).fetchall()
            attempts = db.execute("SELECT a.* FROM attempts a JOIN trials t ON t.id = a.trial_id WHERE t.run_id = ?",
                                  (run_id,)).fetchall()
        return {
            **found[0], "versions": _parse(row["versions"]), "config": _parse(row["config"]),
            "metrics": run_metrics(row, trials, attempts),  # the same numbers `harness results` prints
            "trials": [{**dict(t), "score_value": _parse(t["score_value"]), "attempts": json.loads(t["attempts"] or "[]")}
                       for t in trials],
        }

    def attempt(self, attempt_id: int) -> dict:
        with self._db() as db:
            a = db.execute(ATTEMPT_SQL, (attempt_id,)).fetchone()
            if a is None:
                raise NotFound(f"no attempt {attempt_id}")
            siblings = [dict(s) for s in db.execute("SELECT id, n, outcome FROM attempts WHERE trial_id = ? ORDER BY n",
                                                    (a["trial_id"],))]
            db_events = [{**dict(e), "payload": _parse(e["payload"])} for e in db.execute(
                "SELECT seq, turn, kind, payload, ts FROM events WHERE attempt_id = ? ORDER BY seq", (attempt_id,))]
        folder = self._folder(a["artifacts_dir"])
        events = _read_jsonl(folder / "events.jsonl") if folder else None
        return {
            **{k: a[k] for k in ("id", "trial_id", "run_id", "task_id", "n", "outcome", "exit_status", "error",
                                 "cost_usd", "duration_s", "model_calls", "input_tokens", "output_tokens",
                                 "queued_at", "started_at", "env_ready_at", "agent_start_at", "agent_end_at",
                                 "grade_start_at", "grade_end_at", "finished_at")},
            "score": _parse(a["score"]),
            "attempts": siblings,
            "outcome_file": _read_json(folder / "outcome.json") if folder else None,
            "transcript": _read_json(folder / "transcript.json") if folder else None,
            # events.jsonl is live while the attempt runs; the table only catches up at the end of each turn
            "events": events if events is not None else db_events,
            "files": sorted(({"name": f.name, "bytes": f.stat().st_size} for f in folder.iterdir() if f.is_file()),
                            key=lambda f: f["name"]) if folder else [],
        }

    def attempt_file(self, attempt_id: int, name: str) -> Path:
        if not FILE_NAME.fullmatch(name):
            raise NotFound(name)
        with self._db() as db:
            a = db.execute("SELECT artifacts_dir FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        folder = self._folder(a and a["artifacts_dir"])
        if folder is None or not (folder / name).is_file() or not (folder / name).resolve().is_relative_to(self.runs_dir):
            raise NotFound(name)  # the last check: a symlink in the folder pointing out of runs/
        return folder / name

    def workers(self) -> list[dict]:
        if not self.db_path.exists():
            return []
        with self._db() as db:
            return [dict(w) for w in db.execute(
                "SELECT id, started_at, heartbeat_at FROM workers WHERE heartbeat_at >= ? ORDER BY started_at",
                (time.time() - WORKER_ALIVE_S,))]

    def _folder(self, artifacts_dir: str | None) -> Path | None:
        """The attempt's folder, only if it really is inside runs/ (a DB row is not trusted to point elsewhere)."""
        if not artifacts_dir:
            return None
        folder = Path(artifacts_dir).resolve()
        return folder if folder.is_relative_to(self.runs_dir) and folder.is_dir() else None


class Handler(BaseHTTPRequestHandler):
    dashboard: Dashboard  # set on the subclass make_server builds

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send_json({"error": "forbidden host"}, HTTPStatus.FORBIDDEN)
        path = self.path.split("?", 1)[0]
        parts = path.strip("/").split("/")
        try:
            if path in STATIC_FILES:
                return self._send_file(STATIC_DIR / STATIC_FILES[path])
            if parts == ["api", "runs"]:
                return self._send_json(self.dashboard.runs())
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                return self._send_json(self.dashboard.run(parts[2]))
            if len(parts) == 3 and parts[:2] == ["api", "attempts"] and parts[2].isdigit():
                return self._send_json(self.dashboard.attempt(int(parts[2])))
            if len(parts) == 5 and parts[:2] == ["api", "attempts"] and parts[2].isdigit() and parts[3] == "files":
                return self._send_file(self.dashboard.attempt_file(int(parts[2]), parts[4]))
            if parts == ["api", "workers"]:
                return self._send_json(self.dashboard.workers())
            raise NotFound(path)
        except NotFound as e:
            self._send_json({"error": f"not found: {e}"}, HTTPStatus.NOT_FOUND)
        except (sqlite3.Error, SchemaMismatch) as e:
            self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _host_ok(self) -> bool:
        # a web page elsewhere could point its own hostname at 127.0.0.1 (DNS rebinding); only answer to our names
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _send_json(self, body, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(json.dumps(body, default=str).encode(), "application/json", status)

    def _send_file(self, path: Path) -> None:
        headers = {"Content-Disposition": f'attachment; filename="{path.name}"'} if path.suffix == ".tar" else {}
        self._send(path.read_bytes(), CONTENT_TYPES.get(path.suffix, "text/plain"), HTTPStatus.OK, headers)

    def _send(self, body: bytes, content_type: str, status: HTTPStatus, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass  # the UI polls every 2 s; a line per request would bury everything else


def make_server(db_path: Path, port: int = 8000) -> ThreadingHTTPServer:
    """Bound to 127.0.0.1 only: traces can hold task data that shouldn't reach the network. Port 0 = any free port."""
    handler = type("DashboardHandler", (Handler,), {"dashboard": Dashboard(db_path)})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def _parse(value: str | None):
    return None if value is None else json.loads(value)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):  # not written yet, or being replaced
        return None


def _read_jsonl(path: Path) -> list[dict] | None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:  # the line a running agent is in the middle of appending
            break
    return events
