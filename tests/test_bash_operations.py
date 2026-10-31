"""BashOperations adapter against real Docker: oracle solutions pass, doing nothing fails (skipped if Docker isn't running)."""

import json
import subprocess

import pytest

from agentic_harness.agents import AgentSession
from agentic_harness.benchmarks import BashOperations
from agentic_harness.benchmarks.bash_operations import ROOT
from agentic_harness.environments import DockerEnvironment
from agentic_harness.types import Message, Text
from agentic_harness.environments import docker as docker_module

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)

bench = BashOperations()
TASKS = {t.id: t for t in bench.tasks()}
ORACLES = {t.id: bench.reference_solution(t).script for t in TASKS.values()}  # ours (harness-written): it ships none


@pytest.fixture(scope="module", autouse=True)
def images():
    bench.setup(list(TASKS.values()))


def grade(task, env):
    """What the runner does: grade in a grader attached to the trial's world."""
    with env.attach_grader(bench.grader_image(task)) as grader:
        return bench.grade(task, grader, env, ())


class RecordingSession(AgentSession):
    """A fake agent that remembers what it was sent."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, message: Message) -> Message:
        self.sent.append(message)
        return Message("agent", (Text("done"),))


def test_tasks_match_dataset():
    rows = json.loads((ROOT / "dataset.json").read_text())
    assert [t.id for t in TASKS.values()] == [r["task_id"] for r in rows] and len(rows) == 5
    for t, r in zip(TASKS.values(), rows):
        assert t.benchmark == "bash-operations"
        assert t.image == f"harness-bash-operations:{r['task_id']}-{bench.version()}"
        assert (t.problem_statement, t.base_image) == (r["problem_statement"], r["docker_image"])


def test_run_agent_sends_problem_statement_verbatim():
    task = TASKS["bash-001"]
    session = RecordingSession()
    bench.run_agent(task, session, env=None)  # a simple benchmark never touches the env itself
    assert session.sent == [Message("user", (Text(task.problem_statement),))]


@pytest.mark.parametrize("task_id", ORACLES)
def test_oracle_passes(task_id):
    task = TASKS[task_id]
    with DockerEnvironment(task.image) as env:
        assert env.exec(ORACLES[task_id]).exit_code == 0
        score = grade(task, env)
    assert score.value is True, score.explanation


@pytest.mark.parametrize("task_id", ORACLES)
def test_doing_nothing_fails(task_id):
    task = TASKS[task_id]
    with DockerEnvironment(task.image) as env:
        score = grade(task, env)
    assert score.value is False, score.explanation


def test_world_has_no_python_before_grading():
    with DockerEnvironment(TASKS["bash-001"].image) as env:
        assert env.exec("command -v python3 || command -v python").exit_code != 0
        assert env.exec("ls -A /workspace").stdout == ""  # setup made no files for this task, but the folder exists


def test_world_gains_no_python_and_no_grader_files_from_grading():
    task = TASKS["bash-001"]
    with DockerEnvironment(task.image) as env:
        env.exec(ORACLES["bash-001"])
        assert grade(task, env).value is True
        assert env.exec("command -v python3 || command -v python").exit_code != 0
        assert env.exec("test -e /grader").exit_code != 0  # evaluate.py lived only in the grader


def test_grading_needs_no_internet():
    task = TASKS["bash-001"]
    with DockerEnvironment(task.image) as env:
        env.exec(ORACLES["bash-001"])
        subprocess.run(["docker", "network", "disconnect", "bridge", env.container_id], capture_output=True, check=True)
        with env.attach_grader(bench.grader_image(task)) as grader:  # shares the (now absent) network
            offline = ["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), timeout=2)"]
            assert grader.exec(offline).exit_code != 0
            assert bench.grade(task, grader, env, ()).value is True  # nothing to install: the image has python


def test_setup_files_are_baked_in():
    with DockerEnvironment(TASKS["bash-003"].image) as env:
        assert env.read_file("/workspace/input.txt") == b"line 1\nline 2\nline 3\nline 4\nline 5"


def test_no_leftover_containers_or_volumes():
    for cmd in (["ps", "-a"], ["volume", "ls"]):
        listed = subprocess.run(["docker", *cmd, "-q", "--filter", f"label={docker_module.LABEL}"], capture_output=True, text=True).stdout
        assert listed == ""


def test_reference_solutions_are_labelled_ours():
    assert {bench.reference_solution(t).source for t in TASKS.values()} == {"harness-written"}
