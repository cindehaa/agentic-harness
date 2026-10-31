"""`harness status` and `harness results` read back a database built directly with the Store (no Docker)."""

import json
import time

import pytest
from click.testing import CliRunner

from agentic_harness import cli as cli_module
from agentic_harness.agents import AGENTS, Agent, PreflightError
from agentic_harness.benchmarks import BashOperations
from agentic_harness.benchmarks.base import content_hash
from agentic_harness.cli import cli
from agentic_harness.store import Store
from agentic_harness.types import Outcome, Score

OUTCOMES = {
    "t1": Outcome("graded", Score(True), 1.0, cost_usd=0.02, duration_s=3.0),
    "t2": Outcome("graded", Score(False), 0.0, cost_usd=0.01, duration_s=2.0),
    "t3": Outcome("infra_error", error="DockerError: container is gone", duration_s=1.0),
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "DB_PATH", tmp_path / "harness.db")
    s = Store(tmp_path / "harness.db")
    s.create_run("run-a", "bash-operations", "mini-swe-agent", "haiku", {"harness": "abc"}, {"limits": {}},
                 ["t1", "t2", "t3", "t4"])
    s.worker_started("w")
    s.claim_setup("w", 120)
    s.finish_setup("run-a", "w")
    for task_id, outcome in OUTCOMES.items():
        claim = s.claim_trial("w", 120)
        assert claim.task_id == task_id
        s.finish_attempt(claim, "w", outcome)
    return s


def harness(*args: str) -> str:
    result = CliRunner().invoke(cli, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_status_lists_active_runs_with_progress(store):
    store.create_run("run-b", "python-tasks", "mini-swe-agent", "haiku", {}, {"limits": {}}, ["p1"])
    lines = harness("status").splitlines()
    assert lines[0].split() == ["RUN", "BENCHMARK", "AGENT", "PROGRESS", "SPEND", "ELAPSED", "STARTED"]
    assert lines[1].split()[:6] == ["run-a", "bash-operations", "mini-swe-agent", "3/4", "done", "$0.0300"]
    assert lines[2].split()[:5] == ["run-b", "python-tasks", "mini-swe-agent", "queued", "$0.0000"]


def test_status_shows_spend_against_the_budget_and_elapsed_time(store):
    store.create_run("run-b", "python-tasks", "mini-swe-agent", "haiku", {}, {"limits": {}}, ["p1"], max_cost_usd=2)
    store.db.execute("UPDATE runs SET created_at = 100, status = 'done', finished_at = 190 WHERE id = 'run-b'")
    row = next(line.split() for line in harness("status", "--all").splitlines() if line.startswith("run-b"))
    assert row[3:8] == ["0/1", "done", "$0.0000", "of", "$2.00"] and row[8] == "90.0s"


def test_status_says_when_the_budget_stopped_a_run(store):
    store.db.execute("UPDATE trials SET state = 'done', outcome = 'skipped_budget' WHERE task_id = 't4'")
    assert "4/4 done (budget reached: 1 skipped)" in harness("status")


def test_status_says_when_no_worker_is_running(store):
    store.create_run("run-b", "python-tasks", "mini-swe-agent", "haiku", {}, {"limits": {}}, ["p1"])
    assert "no worker running" not in harness("status")
    store.db.execute("UPDATE workers SET heartbeat_at = 0")  # its last heartbeat is long past: it died
    warning = "2 pending — no worker running; start one with `harness worker`"  # t4 and p1
    assert harness("status").splitlines()[-1] == warning
    assert harness("status", "--run_id", "run-a").splitlines()[-1] == warning
    store.db.execute("UPDATE trials SET state = 'done'")
    assert "no worker running" not in harness("status")  # nothing left to do: no warning


def test_status_hides_runs_finished_over_an_hour_ago_unless_all(store):
    store.db.execute("UPDATE runs SET status = 'done', finished_at = 0")
    assert "No active or recent runs" in harness("status")
    assert "run-a" in harness("status", "--all")


def test_status_of_one_run_shows_its_trials(store):
    out = harness("status", "--run_id", "run-a")
    assert out.splitlines()[0].startswith("Run run-a: bash-operations · mini-swe-agent (haiku), 3/4 done, $0.0300 spent, ")
    rows = [line.split() for line in out.splitlines()[2:]]
    assert rows == [["TASK", "STATE", "OUTCOME", "SCORE", "ATTEMPTS", "COST", "TIME"],
                    ["t1", "done", "graded", "True", "1", "$0.0200", "3.0s"],
                    ["t2", "done", "graded", "False", "1", "$0.0100", "2.0s"],
                    ["t3", "done", "infra_error", "1", "$0.0000", "1.0s"], ["t4", "pending", "0", "$0.0000", "0.0s"]]


def test_status_counts_a_running_trials_time_so_far(store):
    store.claim_trial("w", 120)  # t4
    store.db.execute("UPDATE attempts SET started_at = ? WHERE outcome IS NULL", (time.time() - 30,))
    row = harness("status", "--run_id", "run-a").splitlines()[-1].split()
    assert row[:2] == ["t4", "running"] and 30 <= float(row[-1].rstrip("s")) < 40


def test_results_summary_always_shows_every_excluded_count(store):
    out = harness("results", "--run_id", "run-a", "--format", "summary")
    assert "In progress: 3/4 done" in out
    assert "Accuracy: 0.50 (n=2 graded, 1 passed)" in out
    assert "Excluded: grader_error 0 · infra_error 1 · unsupported 0 · skipped_budget 0" in out
    assert "  infra_error: t3" in out
    assert "Cost: $0.0300 · $0.0100/task · $0.0300/passing task" in out


def finished_with_known_times(store) -> None:
    """t4 run too, then fixed timestamps: setup 100-110; attempt i claimed at 110 + i, its world up 1 s later, its
    agent 2 s, its grading 1 s; done 1 s after (t3's infra_error never got to grading)."""
    store.finish_attempt(store.claim_trial("w", 120), "w", OUTCOMES["t2"])
    store.db.execute("UPDATE runs SET created_at = 90, setup_started_at = 100, setup_finished_at = 110, status = 'done',"
                     " finished_at = 999")
    store.db.execute("UPDATE attempts SET queued_at = 110, started_at = 110 + id, env_ready_at = 111 + id,"
                     " agent_start_at = 111 + id, agent_end_at = 113 + id, grade_start_at = 113 + id, grade_end_at = 114 + id,"
                     " finished_at = 115 + id, input_tokens = 1000 * id, output_tokens = 10 * id, model_calls = id")
    store.db.execute("UPDATE attempts SET grade_start_at = NULL, grade_end_at = NULL WHERE id = 3")


def test_results_summary_breaks_the_time_down(store):
    finished_with_known_times(store)
    out = harness("results", "--run_id", "run-a")
    assert "Tokens: 10,000 in · 100 out · 10 model calls" in out
    assert "Time per trial (avg): env start 1.0s · agent 2.0s · grading 1.0s" in out
    # wall: first claim (111) to last outcome (119); queue wait: 1 + 2 + 3 + 4 s
    assert "Run: setup 10.0s · wall 8.0s (first trial start to last end) · queue wait 10.0s total · 30.0 trials/min" in out


def test_results_default_to_the_summary(store):
    finished_with_known_times(store)
    assert harness("results", "--run_id", "run-a") == harness("results", "--run_id", "run-a", "--format", "summary")


def test_results_table_has_one_row_per_trial(store):
    finished_with_known_times(store)
    rows = [line.split() for line in harness("results", "--run_id", "run-a", "--format", "table").splitlines()]
    assert rows[0] == ["TASK", "OUTCOME", "SCORE", "COST", "TOKENS", "IN/OUT", "AGENT", "GRADING", "TIME", "ATTEMPTS"]
    assert rows[1] == ["t1", "graded", "True", "$0.0200", "1,000/10", "2.0s", "1.0s", "3.0s", "1"]
    assert rows[3] == ["t3", "infra_error", "$0.0000", "3,000/30", "2.0s", "0.0s", "1.0s", "1"]


def test_results_json_has_everything(store):
    report = json.loads(harness("results", "--run_id", "run-a", "--format", "json"))
    assert report["run"]["versions"] == {"harness": "abc"} and report["run"]["config"] == {"limits": {}}
    assert (report["accuracy"], report["graded"]) == (0.5, 2)
    assert report["excluded"] == {"grader_error": [], "infra_error": ["t3"], "unsupported": [], "skipped_budget": []}
    t1 = report["trials"][0]
    assert t1["score_value"] is True and t1["attempts"][0]["score"]["value"] is True
    assert (report["cost_usd"], report["cost_per_task_usd"], report["cost_per_passing_task_usd"]) == (0.03, 0.01, 0.03)
    for key in ("input_tokens", "output_tokens", "model_calls", "avg_agent_s", "avg_grading_s", "avg_env_start_s",
                "total_queue_wait_s", "setup_s", "wall_s", "trials_per_min", "max_cost_usd"):
        assert key in report
    assert {"agent_start_at", "grade_end_at", "queued_at", "input_tokens"} <= set(t1["attempts"][0])


def test_unknown_run_is_an_error(store):
    result = CliRunner().invoke(cli, ["results", "--run_id", "nope"])
    assert result.exit_code != 0 and "no run 'nope'" in result.output


class FakeAgent(Agent):
    name = "fake"

    def start(self, env, artifacts_dir, limits, mcp_servers=()):
        raise NotImplementedError  # never run here


class NoKeyAgent(FakeAgent):
    name = "no-key"

    def preflight(self):
        raise PreflightError("needs FAKE_API_KEY")


@pytest.fixture
def agents(monkeypatch):
    for agent in (FakeAgent, NoKeyAgent):
        monkeypatch.setitem(AGENTS, agent.name, f"{__name__}:{agent.__name__}")
    monkeypatch.setenv("HARNESS_MODEL", "fake/model-1")


def submit(*args: str):
    return CliRunner().invoke(cli, ["run", "--benchmark", "bash-operations", "--task_ids", "bash-001", *args])


def test_run_resolves_the_model_at_submit_and_stores_the_limits(store, agents):
    result = submit("--agent", "fake", "--trial-timeout", "30", "--cost-limit", "0.5", "--max-steps", "7", "--memory", "1g", "--cpus", "2")
    assert result.exit_code == 0, result.output
    run = store.run(result.output.split()[2].rstrip(":"))
    assert run["model"] == "fake/model-1"  # from HARNESS_MODEL, now fixed for the run
    assert json.loads(run["versions"])["benchmark"] == BashOperations().version()
    limits = json.loads(run["config"])["limits"]
    assert (limits["trial_timeout_s"], limits["cost_usd"], limits["max_steps"], limits["memory"], limits["cpus"]) == (30, 0.5, 7, "1g", 2)


def test_model_flag_wins_over_harness_model(store, agents):
    result = submit("--agent", "fake", "--model", "fake/model-2")
    assert result.exit_code == 0, result.output
    assert store.run(result.output.split()[2].rstrip(":"))["model"] == "fake/model-2"


def test_run_without_a_model_fails_clearly(store, agents, monkeypatch):
    monkeypatch.setattr(cli_module, "load_dotenv", lambda *_: None)  # the repo's .env sets HARNESS_MODEL
    monkeypatch.delenv("HARNESS_MODEL")
    monkeypatch.setenv("MSWEA_MODEL_NAME", "fake/mini-cli-model")  # mini's CLI setting: never the harness's default
    result = submit("--agent", "fake")
    assert result.exit_code != 0 and "no model: pass --model or set HARNESS_MODEL in .env" in result.output


def test_run_whose_preflight_fails_submits_nothing(store, agents):
    result = submit("--agent", "no-key")
    assert result.exit_code != 0 and "pre-flight check failed, nothing submitted: needs FAKE_API_KEY" in result.output
    assert [r["id"] for r in store.runs()] == ["run-a"]


def test_content_hash_follows_the_files_and_skips_caches(tmp_path):
    (tmp_path / "setup.py").write_text("v1")
    first = content_hash(tmp_path, "FROM alpine")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "setup.pyc").write_text("junk")
    assert content_hash.__wrapped__(tmp_path, "FROM alpine") == first  # __wrapped__: skip the per-process cache
    (tmp_path / "setup.py").write_text("v2")
    assert content_hash.__wrapped__(tmp_path, "FROM alpine") != first
    assert content_hash.__wrapped__(tmp_path, "FROM debian") != content_hash.__wrapped__(tmp_path, "FROM alpine")
