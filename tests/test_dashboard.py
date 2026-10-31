import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from agentic_harness.dashboard import make_server
from agentic_harness.store import Store
from agentic_harness.types import Outcome, Score


def finish(store: Store, outcome: Outcome, retry_at: float | None = None, events: list[dict] = (),
           run_id: str = "run-done") -> int:
    claim = store.claim_trial("w1", 120, run_id)
    claim.artifacts_dir.mkdir(parents=True)
    (claim.artifacts_dir / "transcript.json").write_text(json.dumps(
        [{"role": "user", "content": [{"text": "do it"}]}, {"role": "agent", "content": [{"text": "done"}]}]))
    (claim.artifacts_dir / "outcome.json").write_text(json.dumps({"status": outcome.status, "score_float": outcome.score_float}))
    (claim.artifacts_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    store.finish_attempt(claim, "w1", outcome, retry_at)
    return claim.attempt_id


T = time.time()
COMMAND = {"seq": 0, "turn": 1, "kind": "command", "ts": 1.0,
           "payload": {"command": "ls", "exit_code": 0, "stdout": "a.txt\n", "stderr": "", "duration_s": 0.1}}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "runs" / "harness.db"
    store = Store(path)
    store.create_run("run-done", "bench", "agent", "model-a", {}, {}, ["t1", "t2", "t3"])
    store.claim_setup("w1", 120, "run-done")
    store.finish_setup("run-done", "w1")
    finish(store, Outcome("graded", Score(True), 1.0, cost_usd=0.25, duration_s=3.0, model_calls=4, input_tokens=1200,
                          output_tokens=300, phases={"env_ready": T + 1, "agent_start": T + 1, "agent_end": T + 3,
                                                     "grade_start": T + 3, "grade_end": T + 3.5}), events=[COMMAND])
    finish(store, Outcome("graded", Score(False), 0.0, cost_usd=0.25, duration_s=2.0))
    finish(store, Outcome("infra_error", error="docker went away", retryable=True), retry_at=0)
    finish(store, Outcome("grader_error", error="unparseable"))
    store.create_run("run-live", "bench", "agent", "model-b", {}, {}, ["t1", "t2"])
    store.claim_setup("w1", 120, "run-live")
    store.finish_setup("run-live", "w1")
    store.claim_trial("w1", 120, "run-live")
    store.create_run("run-budget", "bench", "agent", "model-c", {}, {}, ["t1", "t2"], max_cost_usd=0.1)
    store.claim_setup("w1", 120, "run-budget")
    store.finish_setup("run-budget", "w1")
    finish(store, Outcome("graded", Score(True), 1.0, cost_usd=0.2), run_id="run-budget")
    store.worker_started("host-1")
    store.close()
    return path


@pytest.fixture
def get(db):
    server = make_server(db, port=0)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    port = server.server_address[1]

    def get(path: str, host: str = "127.0.0.1"):
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": f"{host}:{port}"})
        try:
            with urllib.request.urlopen(request) as response:
                body = response.read()
                return response.status, json.loads(body) if response.headers["Content-Type"] == "application/json" else body
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield get
    server.shutdown()
    server.server_close()


def test_runs_list_running_first_with_progress_accuracy_and_cost(get):
    status, runs = get("/api/runs")
    assert status == 200
    assert [r["id"] for r in runs] == ["run-live", "run-budget", "run-done"]
    done = runs[2]
    assert (done["done"], done["total"], done["graded"], done["passed"]) == (3, 3, 2, 1)
    assert (done["grader_error"], done["infra_error"], done["unsupported"]) == (1, 0, 0)
    assert done["accuracy"] == 0.5
    assert done["cost_usd"] == 0.5
    assert (runs[0]["done"], runs[0]["running"]) == (0, 1)


def test_run_detail_has_every_trial_with_its_attempts(get):
    status, run = get("/api/runs/run-done")
    assert status == 200
    trials = {t["task_id"]: t for t in run["trials"]}
    assert trials["t1"]["score_value"] is True and trials["t1"]["cost_usd"] == 0.25
    assert [a["outcome"] for a in trials["t3"]["attempts"]] == ["infra_error", "grader_error"]
    assert trials["t3"]["attempt_count"] == 2


def test_attempt_has_transcript_events_outcome_and_files(get):
    _, run = get("/api/runs/run-done")
    attempt_id = run["trials"][0]["attempts"][0]["id"]
    status, attempt = get(f"/api/attempts/{attempt_id}")
    assert status == 200
    assert attempt["task_id"] == "t1" and attempt["score"]["value"] is True
    assert attempt["transcript"][1]["content"][0]["text"] == "done"
    assert attempt["events"][0]["payload"]["stdout"] == "a.txt\n"
    assert {f["name"] for f in attempt["files"]} == {"events.jsonl", "outcome.json", "transcript.json"}
    status, raw = get(f"/api/attempts/{attempt_id}/files/outcome.json")
    assert status == 200 and raw["status"] == "graded"


def test_retried_attempt_shows_its_error_and_siblings(get):
    _, run = get("/api/runs/run-done")
    first = run["trials"][2]["attempts"][0]["id"]
    _, attempt = get(f"/api/attempts/{first}")
    assert attempt["error"] == "docker went away"
    assert [s["n"] for s in attempt["attempts"]] == [1, 2]


def test_workers_are_the_live_ones(get):
    assert get("/api/workers")[1][0]["id"] == "host-1"


@pytest.mark.parametrize("path", [
    "/api/runs/nope", "/api/runs/..", "/api/attempts/999", "/api/attempts/abc",
    "/api/attempts/1/files/..", "/api/attempts/1/files/%2e%2e%2fharness.db", "/api/attempts/1/files/.hidden",
    "/../pyproject.toml", "/dashboard.py", "/api/attempts/1/files/missing.json",
])
def test_unknown_ids_and_paths_outside_an_attempt_folder_are_not_found(get, path):
    assert get(path)[0] == 404


def test_attempt_folder_outside_runs_is_never_served(db, get, tmp_path):
    (tmp_path / "secret.txt").write_text("x")
    store = Store(db)
    store.db.execute("UPDATE attempts SET artifacts_dir = ? WHERE id = 1", (str(tmp_path),))
    store.close()
    assert get("/api/attempts/1/files/secret.txt")[0] == 404
    assert get("/api/attempts/1")[1]["files"] == []


def test_other_host_names_are_refused(get):
    assert get("/api/runs", host="evil.example")[0] == 403


def test_static_page_is_served(get):
    status, body = get("/")
    assert status == 200 and b'id="root"' in body


def test_no_database_yet_means_no_runs_and_creates_nothing(tmp_path):
    path = tmp_path / "harness.db"
    server = make_server(path, port=0)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/runs") as response:
            assert json.loads(response.read()) == []
    finally:
        server.shutdown()
        server.server_close()
    assert not path.exists()


def test_symlink_out_of_runs_is_not_served(db, get, tmp_path):
    (tmp_path / "secret.txt").write_text("x")
    _, run = get("/api/runs/run-done")
    _, attempt = get(f"/api/attempts/{run['trials'][0]['attempts'][0]['id']}")
    folder = db.parent / "run-done" / "t1" / f"attempt-{attempt['id']}"
    (folder / "link.txt").symlink_to(tmp_path / "secret.txt")
    assert get(f"/api/attempts/{attempt['id']}/files/link.txt")[0] == 404


def test_runs_carry_the_trial_span(get):
    run = get("/api/runs")[1][2]
    assert run["first_started_at"] <= run["last_finished_at"]


def test_database_from_another_schema_version_says_so(db, get):
    store = Store(db)
    store.db.execute("UPDATE schema_version SET version = 1")
    store.close()
    status, body = get("/api/runs")
    assert status == 500 and "schema version 1" in body["error"]


def test_cli_says_when_the_port_is_taken(tmp_path):
    from click.testing import CliRunner

    from agentic_harness.cli import cli

    taken = make_server(tmp_path / "harness.db", port=0)
    try:
        result = CliRunner().invoke(cli, ["dashboard", "--port", str(taken.server_address[1]), "--no-open"])
    finally:
        taken.server_close()
    assert result.exit_code == 1 and "pass another --port" in result.output


def test_run_detail_has_the_results_metrics_and_trial_tokens_and_times(get):
    _, run = get("/api/runs/run-done")
    m = run["metrics"]
    assert (m["input_tokens"], m["output_tokens"], m["model_calls"]) == (1200, 300, 4)
    assert m["cost_per_task_usd"] == 0.5 / 3 and m["cost_per_passing_task_usd"] == 0.5
    assert m["avg_agent_s"] == 2 and m["avg_grading_s"] == 0.5 and m["avg_env_start_s"] is not None
    t1 = run["trials"][0]
    assert (t1["input_tokens"], t1["output_tokens"], t1["agent_s"], t1["grading_s"]) == (1200, 300, 2, 0.5)


def test_budget_and_skipped_budget_trials_are_reported(get):
    _, run = get("/api/runs/run-budget")
    assert run["max_cost_usd"] == 0.1 and run["metrics"]["max_cost_usd"] == 0.1
    assert run["skipped_budget"] == 1 and run["status"] == "done"
    assert [t["outcome"] for t in run["trials"]] == ["graded", "skipped_budget"]
