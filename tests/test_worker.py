import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentic_harness import cli as cli_module
from agentic_harness import worker
from agentic_harness.agents import AGENTS, Agent, AgentSession, AgentStats, PreflightError, TransientError
from agentic_harness.benchmarks import BENCHMARKS, BashOperations
from agentic_harness.cli import cli
from agentic_harness.store import LostClaim, Store
from agentic_harness.types import LimitOverrides, Message, Outcome, Score, Text
from agentic_harness.environments import docker as docker_module

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)


@pytest.fixture(scope="module", autouse=True)
def images():
    bench = BashOperations()
    bench.setup([t for t in bench.tasks() if t.id in ("bash-001", "bash-002")])


class LocalGrade(BashOperations):
    name = "local-bash"
    setups = 0

    def setup(self, tasks):
        LocalGrade.setups += 1

    def grade(self, task, grader, env, transcript):
        return Score(grader.exec(["cat", "hello.txt"]).stdout.strip() == "Hello World")


class BrokenSetup(LocalGrade):
    name = "broken-setup"

    def setup(self, tasks):
        raise RuntimeError("docker build failed: no such base image")


class Oracle(Agent):
    name = "oracle"
    calls = 0

    def start(self, env, artifacts_dir, limits, mcp_servers=()) -> AgentSession:
        agent = self

        class Session(AgentSession):
            def send(self, message: Message) -> Message:
                type(agent).calls += 1
                agent.act(env, message.content[0].text)
                return Message("agent", (Text("done"),))

            def stats(self) -> AgentStats:
                return AgentStats("Submitted", 0.01, 3, 1000, 50)

        return Session()

    def act(self, env, text: str) -> None:
        if "Hello World" in text:
            env.exec("echo 'Hello World' > hello.txt")


class Crashing(Oracle):
    name = "crashing"

    def act(self, env, text):
        raise TransientError("provider down")


class FlakyOnce(Oracle):
    name = "flaky-once"

    def act(self, env, text):
        if type(self).calls == 1:
            raise TransientError("provider blip")
        super().act(env, text)


class Buggy(Oracle):
    name = "buggy"

    def act(self, env, text):
        raise RuntimeError("adapter bug")


class Sleeper(Oracle):
    name = "sleeper"

    def act(self, env, text):
        env.exec("sleep 300", timeout=None)


class Counting(Oracle):
    name = "counting"
    lock = threading.Lock()
    now = peak = 0

    def act(self, env, text):
        with Counting.lock:
            Counting.now += 1
            Counting.peak = max(Counting.peak, Counting.now)
        time.sleep(1)
        with Counting.lock:
            Counting.now -= 1
        super().act(env, text)


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
    for agent in (Oracle, Crashing, FlakyOnce, Buggy, Sleeper, Counting, NoKey):
        monkeypatch.setitem(AGENTS, agent.name, f"{__name__}:{agent.__name__}")
        agent.calls = 0
    LocalGrade.setups = 0
    return tmp_path / "harness.db"


def submit(agent: str, benchmark: str = "local-bash", task_ids: str = "bash-001,bash-002", *extra: str) -> str:
    result = CliRunner().invoke(cli, ["run", "--agent", agent, "--model", "fake-model", "--benchmark", benchmark, "--task_ids", task_ids, *extra])
    assert result.exit_code == 0, result.output
    return result.output.split()[2].rstrip(":")


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
    assert files == {"events.jsonl", "outcome.json", "transcript.json", "workspace.tar"}
    kinds = store.db.execute("SELECT kind FROM events WHERE attempt_id = ? ORDER BY seq", (attempt["id"],)).fetchall()
    assert [k[0] for k in kinds] == ["message_sent", "command", "message_received", "workspace_saved"]


def test_infra_error_is_retried_up_to_three_attempts(db):
    run_id = submit("crashing", task_ids="bash-001")
    work()
    store = Store(db)
    trial = store.trials(run_id)[0]
    assert (trial["state"], trial["outcome"], trial["attempt_count"]) == ("done", "infra_error", 3)
    attempts = store.attempts(run_id)
    assert [a["outcome"] for a in attempts] == ["infra_error"] * 3 and "provider down" in attempts[0]["error"]
    assert len({a["artifacts_dir"] for a in attempts}) == 3


def test_non_retryable_infra_error_fails_fast(db):
    run_id = submit("buggy", task_ids="bash-001")
    work()
    store = Store(db)
    trial = store.trials(run_id)[0]
    assert (trial["state"], trial["outcome"], trial["attempt_count"]) == ("done", "infra_error", 1)
    assert "adapter bug" in store.attempts(run_id)[0]["error"]


def test_a_run_that_fails_preflight_fails_alone_and_other_runs_flow(db):
    store = Store(db)
    store.create_run("bad", "local-bash", "no-key", "fake-model", {}, {"limits": {}}, ["bash-001", "bash-002"])
    store.create_run("unknown-agent", "local-bash", "nobody", "fake-model", {}, {"limits": {}}, ["bash-001"])
    store.create_run("unknown-bench", "no-such-bench", "oracle", "fake-model", {}, {"limits": {}}, ["bash-001"])
    good = submit("oracle", task_ids="bash-001")
    work()
    for run_id, why in (("bad", "needs ANTHROPIC_API_KEY"), ("unknown-agent", "unknown agent 'nobody'"),
                        ("unknown-bench", "unknown benchmark 'no-such-bench'")):
        assert store.run(run_id)["status"] == "failed" and store.run(run_id)["setup_claimed_by"] is None
        assert {(t["state"], t["outcome"], t["attempt_count"]) for t in store.trials(run_id)} == {("done", "infra_error", 1)}
        assert all(why in a["error"] for a in store.attempts(run_id)), [a["error"] for a in store.attempts(run_id)]
    assert store.run(good)["status"] == "done" and store.trials(good)[0]["outcome"] == "graded"


def test_failing_a_run_closes_its_attempt_in_flight(db):
    store = Store(db)
    store.create_run("r1", "local-bash", "oracle", "fake-model", {}, {"limits": {}}, ["bash-001", "bash-002"])
    store.claim_setup("w", 120)
    store.finish_setup("r1", "w")
    claim = store.claim_trial("w", 120)
    assert store.fail_run("r1", "pre-flight check failed: no key") is True
    assert [(t["state"], t["outcome"], t["attempt_count"]) for t in store.trials("r1")] == [("done", "infra_error", 1)] * 2
    assert {a["error"] for a in store.attempts("r1")} == {"pre-flight check failed: no key"}
    with pytest.raises(LostClaim):
        store.finish_attempt(claim, "w", Outcome("graded", Score(True), 1.0))
    assert store.fail_run("r1", "again") is False


def test_an_unknown_benchmark_found_at_setup_fails_the_run(db):
    store = Store(db)
    store.create_run("r1", "no-such-bench", "oracle", "fake-model", {}, {"limits": {}}, ["bash-001"])
    run = store.claim_setup("w", 120)
    worker._setup(store, db, "w", run, worker.Stop())
    assert store.run("r1")["status"] == "failed" and "KeyError" in store.attempts("r1")[0]["error"]


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


def test_run_setup_makes_every_image_a_trial_starts_local(db, monkeypatch):
    pulled, prepared = [], []
    monkeypatch.setattr(worker, "ensure_image", pulled.append)
    monkeypatch.setattr(Oracle, "prepare_image", lambda self, image: prepared.append(image) or image)
    run_id = submit("oracle")
    work()
    assert pulled == ["python:3.11-slim"]
    assert prepared[:2] == [t.image for t in LocalGrade().tasks()[:2]]
    assert Store(db).run(run_id)["status"] == "done"


def test_an_image_that_cant_be_pulled_fails_setup(db, monkeypatch):
    class MissingGrader(LocalGrade):
        name = "missing-grader"

        def grader_image(self, task):
            return "harness-test/no-such-image:never"

    monkeypatch.setitem(BENCHMARKS, MissingGrader.name, MissingGrader)
    run_id = submit("oracle", benchmark="missing-grader")
    work()
    store = Store(db)
    assert store.run(run_id)["status"] == "failed"
    assert "while pulling harness-test/no-such-image:never" in store.attempts(run_id)[0]["error"]
    assert Oracle.calls == 0


def test_a_dead_workers_trial_is_reclaimed_and_finished(db):
    run_id = submit("oracle", task_ids="bash-001")
    store = Store(db)
    store.claim_setup("dead", 120)
    store.finish_setup(run_id, "dead")
    store.claim_trial("dead", lease_s=-1)
    work(concurrency=1)
    trial = store.trials(run_id)[0]
    assert (trial["outcome"], trial["attempt_count"]) == ("graded", 2)
    assert [a["error"] for a in store.attempts(run_id)] == ["worker lost: lease expired", None]


def test_run_wait_runs_only_its_own_run(db):
    other = submit("oracle", task_ids="bash-002")
    result = CliRunner().invoke(cli, ["run", "--agent", "oracle", "--model", "fake-model", "--benchmark", "local-bash", "--task_ids", "bash-001", "--wait"])
    assert result.exit_code == 0, result.output
    assert "Accuracy: 1.00 (n=1 graded, 1 passed)" in result.output
    run_id = result.output.split()[2].rstrip(":")
    store = Store(db)
    assert store.run(run_id)["status"] == "done"
    assert store.run(other)["status"] == "pending"


def test_no_leftover_containers():
    listed = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", f"label={docker_module.LABEL}"], capture_output=True, text=True
    ).stdout
    assert listed == ""


def test_a_worker_is_listed_while_it_runs_and_removed_after(db, monkeypatch):
    submit("oracle", task_ids="bash-001")
    seen = []
    real = worker._work_once

    def spy(store, *args):
        seen.append(store.live_workers(0))
        return real(store, *args)

    monkeypatch.setattr(worker, "_work_once", spy)
    work(concurrency=1)
    assert seen and all(n == 1 for n in seen)
    assert Store(db).live_workers(0) == 0


def _labelled(kind: str) -> list[str]:
    return subprocess.run(["docker", kind, "ls", "-q", *(["-a"] if kind == "container" else []), "--filter", f"label={docker_module.LABEL}"],
                          capture_output=True, text=True).stdout.split()


def _signal_when_running(sig: signal.Signals, containers: int) -> None:
    def run():
        deadline = time.monotonic() + 60
        while len(_labelled("container")) < containers and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(1)
        os.kill(os.getpid(), sig)
    threading.Thread(target=run, daemon=True).start()


def test_ctrl_c_stops_the_worker_and_puts_its_trials_back_in_the_queue(db):
    run_id = submit("sleeper")
    _signal_when_running(signal.SIGINT, containers=2)
    began = time.monotonic()
    result = CliRunner().invoke(cli, ["worker", "--concurrency", "2"])
    assert result.exit_code == 0 and "Worker stopped" in result.output, result.output
    assert time.monotonic() - began < 60
    store = Store(db)
    assert [(t["state"], t["attempt_count"]) for t in store.trials(run_id)] == [("pending", 1)] * 2
    assert [(a["outcome"], a["error"]) for a in store.attempts(run_id)] == [("infra_error", "worker interrupted")] * 2
    assert [a["cost_usd"] for a in store.attempts(run_id)] == [0.01] * 2
    assert store.live_workers(0) == 0
    assert _labelled("container") == [] and _labelled("volume") == []
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_sigterm_stops_run_wait_and_another_worker_finishes_the_run(db, monkeypatch):
    _signal_when_running(signal.SIGTERM, containers=1)
    result = CliRunner().invoke(cli, ["run", "--agent", "sleeper", "--model", "fake-model", "--benchmark", "local-bash",
                                      "--task_ids", "bash-001", "--wait"])
    assert result.exit_code == 130 and "stays queued" in result.output, result.output
    run_id = result.output.split()[2].rstrip(":")
    monkeypatch.setattr(Sleeper, "act", Oracle.act)
    work()
    store = Store(db)
    trial = store.trials(run_id)[0]
    assert (trial["outcome"], trial["attempt_count"]) == ("graded", 2)
    assert [a["error"] for a in store.attempts(run_id)] == ["worker interrupted", None]


def test_a_second_signal_forces_the_exit():
    stop = worker.Stop()
    stop.handle(signal.SIGINT, None)
    assert stop.requested.is_set()
    with pytest.raises(KeyboardInterrupt):
        stop.handle(signal.SIGINT, None)


def test_an_interrupted_setup_is_released_for_another_worker(db):
    store = Store(db)
    store.create_run("r1", "local-bash", "oracle", "fake-model", {}, {"limits": {}}, ["bash-001"])
    stop = worker.Stop()
    stop.track_setup(store.claim_setup("w1", 120)["id"], "w1")
    stop.hand_back(store)
    assert store.run("r1")["setup_claimed_by"] is None
    assert store.claim_setup("w2", 120)["id"] == "r1"


PHASES = ("queued_at", "started_at", "env_ready_at", "agent_start_at", "agent_end_at", "grade_start_at", "grade_end_at",
          "finished_at")


def test_attempts_record_spend_and_phase_timestamps_in_order(db):
    run_id = submit("oracle")
    work()
    store = Store(db)
    run = store.run(run_id)
    assert run["created_at"] <= run["setup_started_at"] <= run["setup_finished_at"]
    for a in store.attempts(run_id):
        times = [a[p] for p in PHASES]
        assert None not in times and times == sorted(times), dict(a)
        assert a["queued_at"] == run["setup_finished_at"]
        assert (a["cost_usd"], a["model_calls"], a["input_tokens"], a["output_tokens"]) == (0.01, 3, 1000, 50)


def test_a_failed_attempts_spend_is_recorded(db):
    run_id = submit("crashing", task_ids="bash-001")
    work()
    attempts = Store(db).attempts(run_id)
    assert [(a["outcome"], a["cost_usd"]) for a in attempts] == [("infra_error", 0.01)] * 3
    assert all(a["agent_end_at"] and a["grade_start_at"] is None for a in attempts)
    assert Store(db).trials(run_id)[0]["cost_usd"] == pytest.approx(0.03)


def test_a_retrys_queue_wait_starts_when_its_backoff_ends(db, monkeypatch):
    monkeypatch.setattr(worker, "RETRY_BACKOFF_S", 0.5)
    run_id = submit("flaky-once", task_ids="bash-001")
    work()
    first, second = Store(db).attempts(run_id)
    assert second["queued_at"] == pytest.approx(first["finished_at"] + 0.5, abs=0.1)
    assert second["queued_at"] <= second["started_at"]


def test_max_run_cost_stops_the_run_once_spent(db):
    run_id = submit("oracle", "local-bash", "bash-001,bash-002", "--max-run-cost", "0.01")
    work(concurrency=1)
    store = Store(db)
    assert store.run(run_id)["max_cost_usd"] == 0.01 and store.run(run_id)["status"] == "done"
    assert [(t["outcome"], t["attempt_count"]) for t in store.trials(run_id)] == [("graded", 1), ("skipped_budget", 0)]
    summary = CliRunner().invoke(cli, ["results", "--run_id", run_id]).output
    assert "skipped_budget 1" in summary and "skipped_budget: bash-002" in summary


def test_max_running_caps_trials_across_two_workers(db, monkeypatch):
    monkeypatch.setattr(worker.socket, "gethostname", lambda: threading.current_thread().name)
    Counting.now = Counting.peak = 0
    run_id = submit("counting")
    workers = [threading.Thread(target=worker.work, args=(db, 2, True), kwargs={"max_running": 1}, name=f"w{i}")
               for i in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(120)
    trials = Store(db).trials(run_id)
    assert [t["outcome"] for t in trials] == ["graded"] * 2 and Counting.peak == 1


def test_worker_max_running_flag_and_env(db):
    submit("oracle", task_ids="bash-001")
    result = CliRunner().invoke(cli, ["worker", "--exit-when-idle"], env={"HARNESS_MAX_RUNNING": "2"})
    assert result.exit_code == 0 and "at most 2 running across all workers" in result.output, result.output
    result = CliRunner().invoke(cli, ["worker", "--exit-when-idle", "--max-running", "0"])
    assert result.exit_code == 2


def test_each_trial_runs_under_defaults_then_the_benchmarks_limits_then_the_flags_given(db, monkeypatch):
    monkeypatch.setattr(LocalGrade, "limits", lambda self, task: LimitOverrides(trial_timeout_s=300, grade_timeout_s=90))
    run_id = submit("oracle", "local-bash", "bash-001", "--grade-timeout", "45", "--max-steps", "9")
    work(1)
    [attempt] = Store(db).attempts(run_id)
    limits = json.loads(attempt["limits"])
    assert limits["trial_timeout_s"] == {"value": 300, "source": "benchmark"}
    assert limits["grade_timeout_s"] == {"value": 45, "source": "flag"}
    assert limits["max_steps"] == {"value": 9, "source": "flag"}
    assert limits["cost_usd"] == {"value": 1.0, "source": "default"}
    assert json.loads((Path(attempt["artifacts_dir"]) / "outcome.json").read_text())["limits"] == limits
    assert json.loads(Store(db).run(run_id)["config"])["non_standard_limits"] == ["grade_timeout_s"]
