"""PythonTasks adapter against real Docker: oracle solutions pass, the untouched stub fails (skipped if Docker isn't running)."""

import json
import subprocess

import pytest

from agentic_harness.agents import AgentSession
from agentic_harness.benchmarks import PythonTasks
from agentic_harness.benchmarks.python_tasks import ROOT
from agentic_harness.environments import DockerEnvironment
from agentic_harness.types import Message, Text

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)

bench = PythonTasks()
TASKS = {t.id: t for t in bench.tasks()}
ORACLES = {t.id: bench.reference_solution(t).files["solution.py"] for t in TASKS.values()}  # ours: it ships none


@pytest.fixture(scope="module", autouse=True)
def images():
    bench.setup(list(TASKS.values()))


def grade(task, env):
    """What the runner does: grade in a grader attached to the trial's world (here the task's own image)."""
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
    assert [t.id for t in TASKS.values()] == [r["id"] for r in rows] and len(rows) == 3
    for t, r in zip(TASKS.values(), rows):
        assert t.benchmark == "python-tasks"
        assert t.image == f"harness-python-tasks:{r['id']}-{bench.version()}"
        assert (t.prompt, t.base_image) == (r["prompt"], r["docker_image"])


def test_run_agent_sends_prompt_verbatim():
    task = TASKS["py-002"]
    session = RecordingSession()
    bench.run_agent(task, session, env=None)  # a simple benchmark never touches the env itself
    assert session.sent == [Message("user", (Text(task.prompt),))]


@pytest.mark.parametrize("task_id", ORACLES)
def test_oracle_passes(task_id):
    task = TASKS[task_id]
    with DockerEnvironment(task.image) as env:
        env.write_file("/workspace/solution.py", ORACLES[task_id])
        score = grade(task, env)
    assert score.value is True, score.explanation


@pytest.mark.parametrize("task_id", ORACLES)
def test_untouched_stub_fails(task_id):
    task = TASKS[task_id]
    with DockerEnvironment(task.image) as env:
        score = grade(task, env)
        assert env.exec("test -e /grader").exit_code != 0  # grade.py lived only in the grader
    assert score.value is False, score.explanation


def test_cheater_scores_as_benchmark_says():
    """Documents the benchmark's own loophole: grade.py runs whatever test_solution.py is in the workspace."""
    task = TASKS["py-001"]
    with DockerEnvironment(task.image) as env:
        env.write_file("/workspace/solution.py", "def add():\n    return 5\n")  # wrong
        env.write_file("/workspace/test_solution.py", "def test_ok(): assert True\n")
        assert grade(task, env).value is True  # faithful: the score is what the benchmark says


def test_world_is_prepared():
    with DockerEnvironment(TASKS["py-001"].image) as env:
        assert env.read_file("/workspace/test_solution.py").decode() == json.loads((ROOT / "dataset.json").read_text())[0]["test_code"]
        assert env.read_file("/workspace/solution.py") == b"# Write your solution here\n"
        assert env.exec(["python", "-c", "import pytest"]).exit_code == 0  # prepare.py's pytest stays for the agent
        assert env.exec("test -e /bench").exit_code != 0  # the benchmark's files (incl. grade.py) are not in the world


def test_no_leftover_containers_or_volumes():
    for cmd in (["ps", "-a"], ["volume", "ls"]):
        listed = subprocess.run(["docker", *cmd, "-q", "--filter", "label=agentic-harness=1"], capture_output=True, text=True).stdout
        assert listed == ""
