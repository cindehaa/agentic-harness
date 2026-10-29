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

# A correct solution.py per task, written from each prompt and its test_code
ORACLES = {
    "py-001": "def add():\n    return 2 + 2\n",
    "py-002": "def fibonacci(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
    "py-003": "def get_nth(lst, n):\n    return lst[n] if 0 <= n < len(lst) else None\n",  # tests want None for -1, not lst[-1]
}

bench = PythonTasks()
TASKS = {t.id: t for t in bench.tasks()}


@pytest.fixture(scope="module", autouse=True)
def images():
    bench.setup(list(TASKS.values()))


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
        assert t.image == f"harness-python-tasks:{r['id']}"
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
        score = bench.grade(task, env, ())
    assert score.value is True, score.explanation


@pytest.mark.parametrize("task_id", ORACLES)
def test_untouched_stub_fails(task_id):
    task = TASKS[task_id]
    with DockerEnvironment(task.image) as env:
        score = bench.grade(task, env, ())
    assert score.value is False, score.explanation


def test_cheater_scores_as_benchmark_says():
    """Documents the benchmark's own loophole: grade.py runs whatever test_solution.py is in the workspace."""
    task = TASKS["py-001"]
    with DockerEnvironment(task.image) as env:
        env.write_file("/workspace/solution.py", "def add():\n    return 5\n")  # wrong
        env.write_file("/workspace/test_solution.py", "def test_ok(): assert True\n")
        assert bench.grade(task, env, ()).value is True  # faithful: the score is what the benchmark says


def test_world_is_prepared():
    with DockerEnvironment(TASKS["py-001"].image) as env:
        assert env.read_file("/workspace/test_solution.py").decode() == json.loads((ROOT / "dataset.json").read_text())[0]["test_code"]
        assert env.read_file("/workspace/solution.py") == b"# Write your solution here\n"
        assert env.exec(["python", "-c", "import pytest"]).exit_code == 0  # prepare.py's pytest stays for the agent
        assert env.exec("test -e /bench").exit_code != 0  # the benchmark's files (incl. grade.py) are not in the world


def test_no_leftover_containers():
    listed = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", "label=agentic-harness=1"], capture_output=True, text=True
    ).stdout
    assert listed == ""
