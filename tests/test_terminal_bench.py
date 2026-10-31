"""TerminalBench adapter against real Docker: reference solutions pass, doing nothing fails (skipped if Docker isn't running)."""

import subprocess

import pytest
import yaml

from agentic_harness.agents import AgentSession
from agentic_harness.benchmarks import GraderError, TerminalBench
from agentic_harness.benchmarks.terminal_bench import ROOT, TASK_IDS, _parse_pytest
from agentic_harness.environments import DockerEnvironment
from agentic_harness.types import Message, Text

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)

bench = TerminalBench()
TASKS = {t.id: t for t in bench.tasks()}


@pytest.fixture(scope="module", autouse=True)
def images():
    bench.setup(list(TASKS.values()))


def grade(task, env):
    """What the runner does: grade in a grader attached to the trial's world."""
    with env.attach_grader(bench.grader_image(task)) as grader:
        return bench.grade(task, grader, env, ())


def apply_reference(task, env):
    ref = bench.reference_solution(task)
    assert env.exec(ref.script, cwd=ref.cwd, timeout=120).exit_code == 0


class RecordingSession(AgentSession):
    """A fake agent that remembers what it was sent."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, message: Message) -> Message:
        self.sent.append(message)
        return Message("agent", (Text("done"),))


def test_tasks_match_task_files():
    assert list(TASKS) == list(TASK_IDS)
    for task_id, t in TASKS.items():
        meta = yaml.safe_load((ROOT / task_id / "task.yaml").read_text())
        assert t.benchmark == "terminal-bench"
        assert t.image == f"harness-terminal-bench:{task_id}-{bench.version()}"
        assert (t.instruction, t.max_test_timeout_s) == (meta["instruction"], float(meta["max_test_timeout_sec"]))


def test_run_agent_sends_instruction_verbatim():
    task = TASKS["hello-world"]
    session = RecordingSession()
    bench.run_agent(task, session, env=None)
    assert session.sent == [Message("user", (Text(task.instruction),))]
    assert task.instruction == 'Create a file called /app/hello.txt. Write "Hello, world!" to it.'


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_reference_solution_passes(task_id):
    task = TASKS[task_id]
    with DockerEnvironment(task.image) as env:
        apply_reference(task, env)
        score = grade(task, env)
    assert score.value is True, score.explanation


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_doing_nothing_fails(task_id):
    task = TASKS[task_id]
    with DockerEnvironment(task.image) as env:
        score = grade(task, env)
    assert score.value is False, score.explanation


def test_reference_solutions_are_the_benchmarks_own():
    assert {bench.reference_solution(t).source for t in TASKS.values()} == {"benchmark-provided"}


def test_tests_enter_the_world_only_at_grade_time():
    task = TASKS["hello-world"]
    with DockerEnvironment(task.image) as env:
        assert env.exec("test -e /tests").exit_code != 0
        assert env.exec("ls /app").stdout.strip() == ""


def test_parser_reads_pytest_summary():
    out = "== short test summary info ==\nPASSED t.py::test_a\nFAILED t.py::test_b - assert 1 == 2\nSKIPPED t.py::test_c\n"
    assert _parse_pytest(out, "") == {"test_a": "PASSED", "test_b": "FAILED", "test_c": "SKIPPED"}


def test_missing_summary_is_a_grader_error():
    with pytest.raises(GraderError):
        _parse_pytest("curl: (6) Could not resolve host", "")


def test_no_leftover_containers_or_volumes():
    for cmd in (["ps", "-a"], ["volume", "ls"]):
        listed = subprocess.run(["docker", *cmd, "-q", "--filter", "label=agentic-harness=1"], capture_output=True, text=True).stdout
        assert listed == ""
