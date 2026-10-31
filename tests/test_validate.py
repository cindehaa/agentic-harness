import subprocess

import pytest
from click.testing import CliRunner

from agentic_harness import cli as cli_module
from agentic_harness.benchmarks import BashOperations
from agentic_harness.cli import cli
from agentic_harness.types import Message, ReferenceSolution, Score, Text
from agentic_harness.validate import validate
from agentic_harness.environments import docker as docker_module

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)


def one_task(bench):
    return [t for t in bench.tasks() if t.id == "bash-001"]


def checks(bench, tmp_path):
    return {c.kind: c for c in validate(bench, one_task(bench), tmp_path)}


def test_correct_adapter_passes_both_checks(tmp_path):
    result = checks(BashOperations(), tmp_path)
    assert (result["null"].ok, result["oracle"].ok, result["oracle"].source) == (True, True, "harness-written")
    assert (tmp_path / "bash-001" / "oracle" / "events.jsonl").exists()


class PassesEverything(BashOperations):
    def grade(self, task, grader, env, transcript):
        return Score(True)


def test_grader_that_passes_everything_fails_the_null_check(tmp_path):
    result = checks(PassesEverything(), tmp_path)
    assert result["null"].ok is False and "doing nothing scored 1.0" in result["null"].detail


class WrongPath(BashOperations):
    def reference_solution(self, task):
        return ReferenceSolution("harness-written", script="mkdir -p sub && echo 'Hello World' > sub/hello.txt")


def test_solution_the_grader_rejects_fails_the_oracle_check(tmp_path):
    result = checks(WrongPath(), tmp_path)
    assert result["null"].ok is True and result["oracle"].ok is False
    assert "reference solution scored 0.0" in result["oracle"].detail


class SilentPrompt(BashOperations):
    def run_agent(self, task, agent, env):
        agent.send(Message("user", (Text("  "),)))


def test_empty_first_message_fails_the_null_check(tmp_path):
    result = checks(SilentPrompt(), tmp_path)
    assert result["null"].ok is False and "first message" in result["null"].detail


class NoSolution(BashOperations):
    def reference_solution(self, task):
        return None


def test_no_reference_solution_is_null_check_only(tmp_path):
    result = checks(NoSolution(), tmp_path)
    assert result["null"].ok is True and result["oracle"].ok is None


def test_cli_exits_1_on_a_failed_check(tmp_path, monkeypatch):
    monkeypatch.setitem(cli_module.BENCHMARKS, "bash-operations", PassesEverything)
    monkeypatch.setattr(cli_module, "RUNS_DIR", tmp_path)
    result = CliRunner().invoke(cli, ["validate", "--benchmark", "bash-operations", "--task_ids", "bash-001"])
    assert result.exit_code == 1 and "FAIL" in result.output and "do-nothing check: doing nothing scored" in result.output


def test_no_leftover_containers_or_volumes():
    for cmd in (["ps", "-a"], ["volume", "ls"]):
        listed = subprocess.run(["docker", *cmd, "-q", "--filter", f"label={docker_module.LABEL}"], capture_output=True, text=True).stdout
        assert listed == ""
