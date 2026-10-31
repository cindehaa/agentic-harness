import json
import threading
import time

import pytest

from agentic_harness import redact as redact_module
from agentic_harness.redact import environ_secrets, redact, secret_values
from agentic_harness.store import LostClaim, SchemaMismatch, Store
from agentic_harness.types import Outcome, Score

GRADED = Outcome("graded", Score(True), 1.0)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "harness.db")
    yield s
    s.close()


def submit(store, run_id="run-1", task_ids=("t1", "t2"), ready=True) -> None:
    store.create_run(run_id, "bench", "agent", "model", {"harness": "abc"}, {"limits": {}}, list(task_ids))
    if ready:
        store.claim_setup("setup-worker", 120, run_id)
        store.finish_setup(run_id, "setup-worker")


def test_schema_version_mismatch_is_refused(tmp_path):
    Store(tmp_path / "harness.db").db.execute("UPDATE schema_version SET version = 999")
    with pytest.raises(SchemaMismatch, match="delete"):
        Store(tmp_path / "harness.db")


def test_setup_is_claimed_once_and_trials_wait_for_it(store, tmp_path):
    submit(store, ready=False)
    other = Store(tmp_path / "harness.db")
    assert store.claim_setup("w1", 120)["id"] == "run-1"
    assert other.claim_setup("w2", 120) is None
    assert store.claim_trial("w1", 120) is None
    store.finish_setup("run-1", "w1")
    assert store.claim_trial("w1", 120).task_id == "t1"


def test_expired_setup_lease_is_reclaimed_and_the_lost_worker_is_ignored(store):
    submit(store, ready=False)
    store.claim_setup("w1", lease_s=-1)
    assert store.claim_setup("w2", 120)["id"] == "run-1"
    assert not store.renew_setup_lease("run-1", "w1", 120)
    with pytest.raises(LostClaim):
        store.finish_setup("run-1", "w1", error="late")
    store.finish_setup("run-1", "w2")
    assert store.run("run-1")["status"] == "running"


def test_setup_failure_fails_the_run_and_its_trials(store):
    submit(store, ready=False)
    store.claim_setup("w1", 120)
    store.finish_setup("run-1", "w1", error="docker build failed")
    assert store.run("run-1")["status"] == "failed"
    assert [(t["state"], t["outcome"]) for t in store.trials("run-1")] == [("done", "infra_error")] * 2
    assert [a["error"] for a in store.attempts("run-1")] == ["setup failed: docker build failed"] * 2
    assert not store.unfinished()


def test_concurrent_claims_take_each_trial_exactly_once(store, tmp_path):
    task_ids = [f"t{i}" for i in range(40)]
    submit(store, task_ids=task_ids)
    claimed: list[str] = []

    def worker(name):
        own = Store(tmp_path / "harness.db")
        while claim := own.claim_trial(name, 120):
            claimed.append(claim.task_id)
        own.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(task_ids)


def test_max_running_caps_claims_across_workers(store, tmp_path):
    submit(store, task_ids=[f"t{i}" for i in range(20)])
    claims = []

    def worker(name):
        own = Store(tmp_path / "harness.db")
        while claim := own.claim_trial(name, 120, max_running=3):
            claims.append((name, claim))
        own.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claims) == 3
    assert store.claim_trial("w9", 120, max_running=3) is None
    name, claim = claims[0]
    store.finish_attempt(claim, name, GRADED)
    assert store.claim_trial("w9", 120, max_running=3) is not None
    assert store.claim_trial("w9", 120, max_running=3) is None


def test_a_running_trial_whose_lease_expired_frees_its_place(store):
    submit(store, task_ids=("t1", "t2"))
    store.claim_trial("dead", lease_s=-1)
    assert store.claim_trial("w", 120, max_running=1).task_id == "t2"
    assert store.claim_trial("w", 120, max_running=1) is None


def test_expired_lease_is_reclaimed_and_the_lost_worker_is_ignored(store):
    submit(store, task_ids=["t1"])
    stale = store.claim_trial("w1", lease_s=-1)
    assert [t["id"] for t in store.reclaim_expired(max_attempts=3, backoff_s=0)] == [stale.trial_id]
    assert store.trials("run-1")[0]["state"] == "pending"

    fresh = store.claim_trial("w2", 120)
    assert (stale.n, fresh.n) == (1, 2)
    assert fresh.artifacts_dir.parts[-3:] == ("run-1", "t1", f"attempt-{fresh.attempt_id}")
    assert fresh.artifacts_dir != stale.artifacts_dir

    assert not store.renew_lease(stale, "w1", 120)
    with pytest.raises(LostClaim):
        store.finish_attempt(stale, "w1", GRADED)
    attempts = store.attempts("run-1")
    assert [(a["outcome"], a["error"]) for a in attempts] == [("infra_error", "worker lost: lease expired"), (None, None)]
    store.finish_attempt(fresh, "w2", GRADED)
    assert store.trials("run-1")[0]["outcome"] == "graded"


def test_lost_workers_count_as_attempts_until_the_trial_ends_infra_error(store):
    submit(store, task_ids=["t1"])
    for _ in range(3):
        store.claim_trial("w1", lease_s=-1)
        store.reclaim_expired(max_attempts=3, backoff_s=0)
    t = store.trials("run-1")[0]
    assert (t["state"], t["outcome"], t["attempt_count"]) == ("done", "infra_error", 3)
    assert store.run("run-1")["status"] == "done"
    assert store.claim_trial("w1", 120) is None


def test_retry_waits_for_its_backoff(store):
    submit(store, task_ids=["t1"])
    claim = store.claim_trial("w1", 120)
    store.finish_attempt(claim, "w1", Outcome("infra_error", error="docker hiccup"), retry_at=time.time() + 60)
    assert store.claim_trial("w1", 120) is None
    store.db.execute("UPDATE trials SET available_at = 0")
    assert store.claim_trial("w1", 120).n == 2


def test_results_accuracy_over_graded_only_and_run_done(store):
    submit(store, task_ids=["t1", "t2", "t3"])
    outcomes = [Outcome("graded", Score(True), 1.0, cost_usd=0.01), Outcome("graded", Score("P"), 0.5),
                Outcome("grader_error", error="crashed")]
    for outcome in outcomes:
        assert store.run("run-1")["status"] == "running"
        store.finish_attempt(store.claim_trial("w1", 120), "w1", outcome)
    assert store.accuracy("run-1") == (0.75, 2)
    run = store.run("run-1")
    assert run["status"] == "done" and run["finished_at"] is not None
    trials = store.trials("run-1")
    assert [json.loads(t["score_value"]) if t["score_value"] else None for t in trials] == [True, "P", None]
    assert trials[0]["cost_usd"] == 0.01
    assert json.loads(store.attempts("run-1")[0]["score"])["value"] is True


def test_runs_lists_active_and_recently_finished(store):
    submit(store, "old", task_ids=["t1"])
    store.db.execute("UPDATE runs SET status = 'done', finished_at = 0 WHERE id = 'old'")
    submit(store, "new", task_ids=["t1", "t2"])
    assert [r["id"] for r in store.runs(finished_since=1)] == ["new"]
    assert [(r["id"], r["done"], r["total"]) for r in store.runs()] == [("old", 0, 1), ("new", 0, 2)]


def test_events_load_per_turn_and_again_at_the_end_without_duplicates(store):
    submit(store, task_ids=["t1"])
    claim = store.claim_trial("w1", 120)
    claim.artifacts_dir.mkdir(parents=True)
    path = claim.artifacts_dir / "events.jsonl"
    path.write_text("".join(json.dumps({"seq": i, "turn": 1, "kind": "k", "payload": {"i": i}, "ts": 0}) + "\n" for i in range(2)))
    store.load_events(claim.attempt_id, path)
    store.finish_attempt(claim, "w1", GRADED)
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_secret_values_from_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY='sk-ant-12345678'\nMSWEA_MODEL_NAME=anthropic/claude\nMY_TOKEN=short\n"
                   "# DB_PASSWORD=commented-out\nexport DB_PASSWORD=hunter2hunter2\n")
    secrets = secret_values(env)
    assert secrets == ("sk-ant-12345678", "hunter2hunter2")
    assert redact("key sk-ant-12345678 and hunter2hunter2, model anthropic/claude", secrets) == "key *** and ***, model anthropic/claude"


def test_secret_named_variables_in_the_environment_are_redacted(monkeypatch):
    monkeypatch.setenv("SHELL_ONLY_API_TOKEN", "tok-from-the-shell-123")
    monkeypatch.setenv("SHORT_KEY", "abc")
    monkeypatch.setenv("HARNESS_TEST_GREETING", "not-a-secret-value")
    assert environ_secrets({"A_PASSWORD": "pw-123456", "PLAIN": "pw-123456x"}) == {"pw-123456"}
    assert redact("tok-from-the-shell-123 abc not-a-secret-value") == "*** abc not-a-secret-value"


def test_forwarded_variables_are_redacted_whatever_their_name(monkeypatch):
    monkeypatch.setenv("HARNESS_TEST_FORWARDED", "value-handed-to-the-world")
    monkeypatch.setattr(redact_module, "_forwarded", set())
    assert redact("value-handed-to-the-world") == "value-handed-to-the-world"
    redact_module.add_forwarded(["HARNESS_TEST_FORWARDED", "HARNESS_TEST_UNSET"])
    assert redact("got value-handed-to-the-world") == "got ***"


def test_recorded_spend_is_never_erased_by_an_outcome_without_it(store):
    from agentic_harness.agents import AgentStats

    submit(store, task_ids=("t1",))
    claim = store.claim_trial("w", 120)
    store.record_spend(claim.attempt_id, AgentStats(None, 0.05, 4, 900, 60))
    store.finish_attempt(claim, "w", Outcome("infra_error", error="worker interrupted"))
    a = store.attempts("run-1")[0]
    assert (a["cost_usd"], a["model_calls"], a["input_tokens"], a["output_tokens"]) == (0.05, 4, 900, 60)


def budgeted(store, task_ids=("t1", "t2", "t3"), budget=0.05) -> None:
    store.create_run("run-b", "bench", "agent", "model", {}, {"limits": {}}, list(task_ids), max_cost_usd=budget)
    store.claim_setup("w", 120, "run-b")
    store.finish_setup("run-b", "w")


def test_a_spent_budget_stops_claiming_and_skips_the_waiting_trials(store):
    budgeted(store)
    claim = store.claim_trial("w", 120)
    store.finish_attempt(claim, "w", Outcome("graded", Score(True), 1.0, cost_usd=0.05))
    assert store.claim_trial("w", 120) is None
    trials = store.trials("run-b")
    assert [(t["state"], t["outcome"], t["attempt_count"]) for t in trials] == [
        ("done", "graded", 1), ("done", "skipped_budget", 0), ("done", "skipped_budget", 0)]
    assert len(store.attempts("run-b")) == 1
    assert store.run("run-b")["status"] == "done"


def test_under_budget_trials_go_on(store):
    budgeted(store)
    store.finish_attempt(store.claim_trial("w", 120), "w", Outcome("graded", Score(True), 1.0, cost_usd=0.01))
    assert store.claim_trial("w", 120).task_id == "t2"


def test_trials_in_flight_finish_when_live_spend_reaches_the_budget(store):
    from agentic_harness.agents import AgentStats

    budgeted(store)
    first, second = store.claim_trial("w1", 120), store.claim_trial("w2", 120)
    store.record_spend(first.attempt_id, AgentStats(None, 0.06))
    assert store.trials("run-b")[2]["outcome"] == "skipped_budget"
    assert store.claim_trial("w3", 120) is None
    store.finish_attempt(first, "w1", Outcome("graded", Score(True), 1.0, cost_usd=0.06))
    assert store.run("run-b")["status"] == "running"
    store.finish_attempt(second, "w2", Outcome("graded", Score(False), 0.0, cost_usd=0.02))
    assert [t["outcome"] for t in store.trials("run-b")] == ["graded", "graded", "skipped_budget"]
    assert store.run("run-b")["status"] == "done"


def test_a_retry_waiting_when_the_budget_runs_out_is_skipped(store):
    budgeted(store, task_ids=("t1",))
    claim = store.claim_trial("w", 120)
    store.finish_attempt(claim, "w", Outcome("infra_error", cost_usd=0.05, retryable=True), retry_at=time.time())
    trial = store.trials("run-b")[0]
    assert (trial["state"], trial["outcome"], trial["attempt_count"]) == ("done", "skipped_budget", 1)
