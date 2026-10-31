import os
import subprocess
import time

import pytest

from agentic_harness.benchmarks import BashOperations
from agentic_harness.parity import original_side, overlap, pass_rates, summarize
from agentic_harness.types import OriginalResult, Score
from agentic_harness.environments import docker as docker_module


def test_summarize_mean_sem_and_range():
    s = summarize([{"a": 1.0, "b": 0.0}, {"a": 1.0, "b": 1.0}, {"a": 1.0, "b": None}])
    assert s["run_accuracies"] == [0.5, 1.0, 1.0]
    assert s["mean"] == pytest.approx(2.5 / 3) and s["range"] == [0.5, 1.0]
    assert s["sem"] == pytest.approx(0.288675 / 3 ** 0.5)


def test_one_run_has_no_sem_and_no_runs_no_mean():
    assert summarize([{"a": 1.0}])["sem"] is None
    assert summarize([{"a": None}]) == {"run_accuracies": [], "mean": None, "sem": None, "range": None}


def test_pass_rates_per_task():
    assert pass_rates([{"a": 1.0, "b": 0.0}, {"a": 0.0, "b": None}]) == {"a": 0.5, "b": 0.0}


def test_overlap():
    assert overlap([0.6, 0.8], [0.8, 1.0]) is True
    assert overlap([0.2, 0.4], [0.6, 1.0]) is False
    assert overlap(None, [0.6, 1.0]) is None


class Canned(BashOperations):
    def original_workflow(self, task, agent_name, model, out_dir):
        if task.id == "bash-003":
            time.sleep(0.05 * (12 - int(out_dir.parent.name.removeprefix("run-"))))
            raise RuntimeError("setup.py failed")
        return OriginalResult(Score(task.id == "bash-001"), cost_usd=0.01)


def test_original_side_scores_costs_and_errors(tmp_path):
    bench = Canned()
    tasks = [t for t in bench.tasks() if t.id in ("bash-001", "bash-002", "bash-003")]
    scores, cost, errors = original_side(bench, tasks, "mini-swe-agent", "m", 11, tmp_path, concurrency=11)
    assert scores == [{"bash-001": 1.0, "bash-002": 0.0, "bash-003": None}] * 11
    assert cost == pytest.approx(0.22)
    assert errors == [f"run {i} bash-003: RuntimeError: setup.py failed" for i in range(1, 12)]


def test_no_original_workflow_for_other_agents(tmp_path):
    task = BashOperations().tasks()[0]
    with pytest.raises(NotImplementedError, match="claude-code"):
        BashOperations().original_workflow(task, "claude-code", "m", tmp_path)


@pytest.mark.live
@pytest.mark.skipif(not (os.getenv("RUN_LIVE") == "1" and os.getenv("ANTHROPIC_API_KEY")), reason="needs RUN_LIVE=1 and an API key")
def test_live_original_workflow_bash_001(tmp_path):
    bench = BashOperations()
    task = bench.tasks()[0]
    result = bench.original_workflow(task, "mini-swe-agent", os.environ["HARNESS_MODEL"], tmp_path)
    assert result.score.value is True and result.cost_usd > 0
    assert (tmp_path / "trajectory.json").exists()
    left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={docker_module.LABEL}"], capture_output=True, text=True).stdout
    assert left == ""
