"""`harness status` and `harness results` read back a database built directly with the Store (no Docker)."""

import json

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
    assert lines[0].split() == ["RUN", "BENCHMARK", "AGENT", "PROGRESS", "STARTED"]
    assert lines[1].split()[:5] == ["run-a", "bash-operations", "mini-swe-agent", "3/4", "done"]
    assert lines[2].split()[:4] == ["run-b", "python-tasks", "mini-swe-agent", "queued"]


def test_status_says_when_no_worker_is_running(store):
    store.create_run("run-b", "python-tasks", "mini-swe-agent", "haiku", {}, {"limits": {}}, ["p1"])
    assert "no worker running" not in harness("status")
    store.db.execute("UPDATE workers SET heartbeat_at = 0")  # its last heartbeat is long past: it died
    lines = harness("status").splitlines()
    assert "queued - no worker running (start one: harness worker)" in lines[2]
    assert "3/4 done - no worker running" in harness("status", "--run_id", "run-a")


def test_status_hides_runs_finished_over_an_hour_ago_unless_all(store):
    store.db.execute("UPDATE runs SET status = 'done', finished_at = 0")
    assert "No active or recent runs" in harness("status")
    assert "run-a" in harness("status", "--all")


def test_status_of_one_run_shows_its_trials(store):
    out = harness("status", "--run_id", "run-a")
    assert out.splitlines()[0] == "Run run-a: bash-operations · mini-swe-agent (haiku), 3/4 done"
    rows = [line.split() for line in out.splitlines()[2:]]
    assert rows == [["TASK", "STATE", "OUTCOME", "SCORE", "ATTEMPTS"], ["t1", "done", "graded", "True", "1"],
                    ["t2", "done", "graded", "False", "1"], ["t3", "done", "infra_error", "1"], ["t4", "pending", "0"]]


def test_results_summary_always_shows_every_excluded_count(store):
    out = harness("results", "--run_id", "run-a", "--format", "summary")
    assert "In progress: 3/4 done" in out
    assert "Accuracy: 0.50 (n=2 graded)" in out
    assert "Excluded: grader_error 0 · infra_error 1 · unsupported 0" in out
    assert "  infra_error: t3" in out
    assert "Cost: $0.0300 · Time: 6.0s in trials" in out


def test_results_table_has_one_row_per_trial(store):
    rows = [line.split() for line in harness("results", "--run_id", "run-a", "--format", "table").splitlines()]
    assert rows[0] == ["TASK", "OUTCOME", "SCORE", "COST", "TIME", "ATTEMPTS"]
    assert rows[1] == ["t1", "graded", "True", "$0.0200", "3.0s", "1"]
    assert rows[4] == ["t4", "pending", "$0.0000", "0.0s", "0"]


def test_results_json_has_everything(store):
    report = json.loads(harness("results", "--run_id", "run-a", "--format", "json"))
    assert report["run"]["versions"] == {"harness": "abc"} and report["run"]["config"] == {"limits": {}}
    assert (report["accuracy"], report["graded"]) == (0.5, 2)
    assert report["excluded"] == {"grader_error": [], "infra_error": ["t3"], "unsupported": []}
    t1 = report["trials"][0]
    assert t1["score_value"] is True and t1["attempts"][0]["score"]["value"] is True


def test_unknown_run_is_an_error(store):
    result = CliRunner().invoke(cli, ["results", "--run_id", "nope"])
    assert result.exit_code != 0 and "no run 'nope'" in result.output


class FakeAgent(Agent):
    name = "fake"
    model_env = "FAKE_MODEL"

    def start(self, env, artifacts_dir, limits):
        raise NotImplementedError  # never run here


class NoKeyAgent(FakeAgent):
    name = "no-key"

    def preflight(self):
        raise PreflightError("needs FAKE_API_KEY")


@pytest.fixture
def agents(monkeypatch):
    for agent in (FakeAgent, NoKeyAgent):
        monkeypatch.setitem(AGENTS, agent.name, f"{__name__}:{agent.__name__}")
    monkeypatch.setenv("FAKE_MODEL", "fake/model-1")


def submit(*args: str):
    return CliRunner().invoke(cli, ["run", "--benchmark", "bash-operations", "--task_ids", "bash-001", *args])


def test_run_resolves_the_model_at_submit_and_stores_the_limits(store, agents):
    result = submit("--agent", "fake", "--trial-timeout", "30", "--cost-limit", "0.5", "--max-steps", "7", "--memory", "1g", "--cpus", "2")
    assert result.exit_code == 0, result.output
    run = store.run(result.output.split()[2].rstrip(":"))
    assert run["model"] == "fake/model-1"  # from FAKE_MODEL, now fixed for the run
    assert json.loads(run["versions"])["benchmark"] == BashOperations().version()
    limits = json.loads(run["config"])["limits"]
    assert (limits["trial_timeout_s"], limits["cost_usd"], limits["max_steps"], limits["memory"], limits["cpus"]) == (30, 0.5, 7, "1g", 2)


def test_run_without_a_model_fails_clearly(store, agents, monkeypatch):
    monkeypatch.delenv("FAKE_MODEL")
    result = submit("--agent", "fake")
    assert result.exit_code != 0 and "no model: pass --model or set FAKE_MODEL in .env" in result.output


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
