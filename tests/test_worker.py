"""Submit with `harness run`, run with `harness worker`: scripted fake agents, real Docker, no model.

Grading reads hello.txt directly (no `apk add`), so these tests don't need the network."""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentic_harness import cli as cli_module
from agentic_harness import worker
from agentic_harness.agents import AGENTS, Agent, AgentSession, PreflightError, TransientError
from agentic_harness.benchmarks import BENCHMARKS, BashOperations
from agentic_harness.cli import cli
from agentic_harness.store import Store
from agentic_harness.types import Message, Score, Text

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)


@pytest.fixture(scope="module", autouse=True)
def images():
    bench = BashOperations()
    bench.setup([t for t in bench.tasks() if t.id in ("bash-001", "bash-002")])


class LocalGrade(BashOperations):
    """bash-operations with a grader that needs no network: passed = hello.txt says Hello World."""

    name = "local-bash"
    setups = 0

    def setup(self, tasks):
        LocalGrade.setups += 1  # the images were built by the fixture

    def grade(self, task, env, transcript):
        return Score(env.exec(["cat", "hello.txt"]).stdout.strip() == "Hello World")


class BrokenSetup(LocalGrade):
    name = "broken-setup"

    def setup(self, tasks):
        raise RuntimeError("docker build failed: no such base image")


class Oracle(Agent):
    """Writes hello.txt only when asked for Hello World (bash-001), so bash-002 fails."""

    name = "oracle"
    calls = 0

    def __init__(self, model=None) -> None:
        self.model = model or "fake-model"

    def start(self, env, artifacts_dir, limits) -> AgentSession:
        agent = self

        class Session(AgentSession):
            def send(self, message: Message) -> Message:
                type(agent).calls += 1
                agent.act(env, message.content[0].text)
                self.exit_status, self.cost_usd = "Submitted", 0.01
                return Message("agent", (Text("done"),))

        return Session()

    def act(self, env, text: str) -> None:
        if "Hello World" in text:
            env.exec("echo 'Hello World' > hello.txt")


class Crashing(Oracle):
    name = "crashing"

    def act(self, env, text):
        raise TransientError("provider down")  # -> infra_error, retryable


class FlakyOnce(Oracle):
    name = "flaky-once"

    def act(self, env, text):
        if type(self).calls == 1:
            raise TransientError("provider blip")
        super().act(env, text)


class Buggy(Oracle):
    name = "buggy"

    def act(self, env, text):
        raise RuntimeError("adapter bug")  # -> infra_error, not retryable


class NoKey(Oracle):
    name = "no-key"

    def preflight(self):
        raise PreflightError("needs ANTHROPIC_API_KEY")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "DB_PATH", tmp_path / "harness.db")
    monkeypatch.setattr(worker, "IDLE_SLEEP_S", 0.05)
    monkeypatch.setattr(worker, "RETRY_BACKOFF_S", 0)
    for bench in (LocalGrade, BrokenSetup):
        monkeypatch.setitem(BENCHMARKS, bench.name, bench)
    for agent in (Oracle, Crashing, FlakyOnce, Buggy, NoKey):
        monkeypatch.setitem(AGENTS, agent.name, f"{__name__}:{agent.__name__}")  # registry entries are "module:Class"
        agent.calls = 0
    LocalGrade.setups = 0
    return tmp_path / "harness.db"


def submit(agent: str, benchmark: str = "local-bash", task_ids: str = "bash-001,bash-002", *extra: str) -> str:
    result = CliRunner().invoke(cli, ["run", "--agent", agent, "--benchmark", benchmark, "--task_ids", task_ids, *extra])
    assert result.exit_code == 0, result.output
    return result.output.split()[2].rstrip(":")  # "Submitted run <id>: ..."


def work(concurrency: int = 2) -> None:
    result = CliRunner().invoke(cli, ["worker", "--concurrency", str(concurrency), "--exit-when-idle"])
    assert result.exit_code == 0, result.output


def test_submit_returns_at_once_and_a_worker_runs_the_trials(db):
    run_id = submit("oracle")
    store = Store(db)
    run = store.run(run_id)
    assert (run["status"], run["agent"], run["model"]) == ("pending", "oracle", "fake-model")
    assert json.loads(run["versions"])["harness"] and json.loads(run["config"])["limits"]["max_steps"] == 50
    assert [t["state"] for t in store.trials(run_id)] == ["pending", "pending"]

    work()
    assert LocalGrade.setups == 1
    assert store.run(run_id)["status"] == "done"
    trials = store.trials(run_id)
    assert [(t["task_id"], t["outcome"], json.loads(t["score_value"]), t["attempt_count"]) for t in trials] == [
        ("bash-001", "graded", True, 1), ("bash-002", "graded", False, 1)]
    attempt = store.attempts(run_id)[0]
    files = {p.name for p in Path(attempt["artifacts_dir"]).iterdir()}
    assert files == {"events.jsonl", "outcome.json", "transcript.json"}
    kinds = store.db.execute("SELECT kind FROM events WHERE attempt_id = ? ORDER BY seq", (attempt["id"],)).fetchall()
    assert [k[0] for k in kinds] == ["message_sent", "message_received"]


def test_infra_error_is_retried_up_to_three_attempts(db):
    run_id = submit("crashing", task_ids="bash-001")
    work()
    store = Store(db)
    trial = store.trials(run_id)[0]
    assert (trial["state"], trial["outcome"], trial["attempt_count"]) == ("done", "infra_error", 3)
    attempts = store.attempts(run_id)
    assert [a["outcome"] for a in attempts] == ["infra_error"] * 3 and "provider down" in attempts[0]["error"]
    assert len({a["artifacts_dir"] for a in attempts}) == 3  # each attempt has its own folder


def test_non_retryable_infra_error_fails_fast(db):
    run_id = submit("buggy", task_ids="bash-001")
    work()
    store = Store(db)
    trial = store.trials(run_id)[0]
    assert (trial["state"], trial["outcome"], trial["attempt_count"]) == ("done", "infra_error", 1)
    assert "adapter bug" in store.attempts(run_id)[0]["error"]


def test_worker_preflight_failure_claims_nothing(db):
    store = Store(db)
    store.create_run("r1", "local-bash", "no-key", "fake-model", {}, {"limits": {}}, ["bash-001"])
    result = CliRunner().invoke(cli, ["worker", "--exit-when-idle"])
    assert result.exit_code != 0 and "pre-flight check failed" in result.output and "needs ANTHROPIC_API_KEY" in result.output
    assert store.run("r1")["setup_claimed_by"] is None and store.trials("r1")[0]["state"] == "pending"


def test_a_retry_that_succeeds_is_graded(db):
    run_id = submit("flaky-once", task_ids="bash-001")
    work()
    store = Store(db)
    trial = store.trials(run_id)[0]
    assert (trial["outcome"], trial["score_float"], trial["attempt_count"]) == ("graded", 1.0, 2)
    assert [a["outcome"] for a in store.attempts(run_id)] == ["infra_error", "graded"]


def test_setup_failure_fails_the_run(db):
    run_id = submit("oracle", benchmark="broken-setup")
    work()
    store = Store(db)
    assert store.run(run_id)["status"] == "failed"
    assert {(t["state"], t["outcome"]) for t in store.trials(run_id)} == {("done", "infra_error")}
    assert "no such base image" in store.attempts(run_id)[0]["error"]
    assert "no such base image" in (db.parent / run_id / "setup.log").read_text()
    assert Oracle.calls == 0


def test_a_dead_workers_trial_is_reclaimed_and_finished(db):
    run_id = submit("oracle", task_ids="bash-001")
    store = Store(db)
    store.claim_setup("dead", 120)
    store.finish_setup(run_id, "dead")
    store.claim_trial("dead", lease_s=-1)  # claimed, then the worker was killed: its lease is already over
    work(concurrency=1)
    trial = store.trials(run_id)[0]
    assert (trial["outcome"], trial["attempt_count"]) == ("graded", 2)
    assert [a["error"] for a in store.attempts(run_id)] == ["worker lost: lease expired", None]


def test_run_wait_runs_only_its_own_run(db):
    other = submit("oracle", task_ids="bash-002")
    result = CliRunner().invoke(cli, ["run", "--agent", "oracle", "--benchmark", "local-bash", "--task_ids", "bash-001", "--wait"])
    assert result.exit_code == 0, result.output
    assert "Accuracy: 1.00 (n=1 graded)" in result.output  # it prints the results
    run_id = result.output.split()[2].rstrip(":")
    store = Store(db)
    assert store.run(run_id)["status"] == "done"
    assert store.run(other)["status"] == "pending"  # left for the workers


def test_no_leftover_containers():
    listed = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", "label=agentic-harness=1"], capture_output=True, text=True
    ).stdout
    assert listed == ""
