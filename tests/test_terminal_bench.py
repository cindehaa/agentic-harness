import subprocess

import pytest
import yaml
from click.testing import CliRunner

from agentic_harness.agents import AgentSession
from agentic_harness.benchmarks import BenchmarkUnavailable, GraderError, TerminalBench
from agentic_harness.benchmarks import terminal_bench as terminal_bench_module
from agentic_harness.benchmarks.terminal_bench import ROOT, TASK_IDS, WORKDIR, _parse_pytest
from agentic_harness.cli import cli
from agentic_harness.environments import DockerEnvironment
from agentic_harness.types import Message, Text
from agentic_harness.environments import docker as docker_module

bench = TerminalBench()
needs_world = pytest.mark.skipif(
    not ROOT.is_dir() or subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
    reason="needs the terminal-bench submodule (make init-extra-benchmarks BENCH=benchmarks/terminal-bench) and Docker",
)


@pytest.fixture(scope="module")
def tasks():
    by_id = {t.id: t for t in bench.tasks()}
    bench.setup(list(by_id.values()))
    return by_id


def grade(task, env):
    with env.attach_grader(bench.grader_image(task)) as grader:
        return bench.grade(task, grader, env, ())


def apply_reference(task, env):
    ref = bench.reference_solution(task)
    for path, text in ref.files.items():
        env.write_file(path, text)
    assert env.exec(ref.script, cwd=ref.cwd, timeout=120).exit_code == 0


class RecordingSession(AgentSession):
    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, message: Message) -> Message:
        self.sent.append(message)
        return Message("agent", (Text("done"),))


@needs_world
def test_tasks_match_task_files(tasks):
    assert list(tasks) == list(TASK_IDS)
    for task_id, t in tasks.items():
        meta = yaml.safe_load((ROOT / task_id / "task.yaml").read_text())
        assert t.benchmark == "terminal-bench"
        assert t.image == f"harness-terminal-bench:{task_id}-{bench.version()}"
        assert (t.instruction, t.max_test_timeout_s) == (meta["instruction"], float(meta["max_test_timeout_sec"]))


@pytest.mark.skipif(not ROOT.is_dir(), reason="needs the terminal-bench submodule (make init-extra-benchmarks BENCH=benchmarks/terminal-bench)")
def test_task_yaml_timeouts_are_the_trial_and_grade_timeouts():
    for t in bench.tasks():
        meta = yaml.safe_load((ROOT / t.id / "task.yaml").read_text())
        declared = bench.limits(t)
        assert declared.trial_timeout_s == float(meta["max_agent_timeout_sec"])
        assert declared.grade_timeout_s == float(meta["max_test_timeout_sec"])
        assert set(declared.given()) == {"trial_timeout_s", "grade_timeout_s"}


def test_task_yaml_without_timeouts_gets_terminal_benchs_defaults(tmp_path, monkeypatch):
    for task_id in TASK_IDS:
        (tmp_path / task_id).mkdir()
        (tmp_path / task_id / "task.yaml").write_text("instruction: do it\n")
    monkeypatch.setattr(terminal_bench_module, "ROOT", tmp_path)
    monkeypatch.setattr(TerminalBench, "version", lambda self: "v")
    declared = bench.limits(bench.tasks()[0])
    assert (declared.trial_timeout_s, declared.grade_timeout_s) == (360.0, 60.0)


@needs_world
def test_run_agent_sends_instruction_verbatim(tasks):
    task = tasks["hello-world"]
    session = RecordingSession()
    bench.run_agent(task, session, env=None)
    assert session.sent == [Message("user", (Text(task.instruction),))]
    assert task.instruction == 'Create a file called /app/hello.txt. Write "Hello, world!" to it.'


@needs_world
@pytest.mark.parametrize("task_id", TASK_IDS)
def test_reference_solution_passes(task_id, tasks):
    task = tasks[task_id]
    with DockerEnvironment(task.image, workdir=task.workdir) as env:
        apply_reference(task, env)
        score = grade(task, env)
    assert score.value is True, score.explanation


@needs_world
@pytest.mark.parametrize("task_id", TASK_IDS)
def test_doing_nothing_fails(task_id, tasks):
    task = tasks[task_id]
    with DockerEnvironment(task.image, workdir=task.workdir) as env:
        score = grade(task, env)
    assert score.value is False, score.explanation


@needs_world
def test_reference_solutions_are_the_benchmarks_own(tasks):
    assert {bench.reference_solution(t).source for t in tasks.values()} == {"benchmark-provided"}


@needs_world
@pytest.mark.parametrize("task_id", TASK_IDS)
def test_images_work_in_the_assumed_workdir(task_id, tasks):
    workdir = subprocess.run(["docker", "image", "inspect", "-f", "{{.Config.WorkingDir}}", tasks[task_id].image],
                             capture_output=True, text=True, check=True).stdout.strip()
    assert workdir == WORKDIR == tasks[task_id].workdir


@needs_world
def test_tests_enter_the_world_only_at_grade_time(tasks):
    task = tasks["hello-world"]
    with DockerEnvironment(task.image, workdir=task.workdir) as env:
        assert env.exec("test -e /tests").exit_code != 0
        assert env.exec("ls /app").stdout.strip() == ""
        assert env.exec("pwd").stdout.strip() == "/app"


def test_parser_reads_pytest_summary():
    out = "== short test summary info ==\nPASSED t.py::test_a\nFAILED t.py::test_b - assert 1 == 2\nSKIPPED t.py::test_c\n"
    assert _parse_pytest(out, "") == {"test_a": "PASSED", "test_b": "FAILED", "test_c": "SKIPPED"}


def test_missing_summary_is_a_grader_error():
    with pytest.raises(GraderError):
        _parse_pytest("curl: (6) Could not resolve host", "")


@needs_world
def test_no_leftover_containers_or_volumes():
    for cmd in (["ps", "-a"], ["volume", "ls"]):
        listed = subprocess.run(["docker", *cmd, "-q", "--filter", f"label={docker_module.LABEL}"], capture_output=True, text=True).stdout
        assert listed == ""


def test_missing_submodule_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal_bench_module, "ROOT", tmp_path / "original-tasks")
    with pytest.raises(BenchmarkUnavailable, match="make init-extra-benchmarks BENCH=benchmarks/terminal-bench"):
        TerminalBench().tasks()
    for command in (["run", "--agent", "mini-swe-agent", "--model", "anthropic/claude-haiku-5-5"], ["validate"]):
        result = CliRunner().invoke(cli, [*command, "--benchmark", "terminal-bench"], env={"ANTHROPIC_API_KEY": "sk-test-0000"})
        assert result.exit_code == 1 and "make init-extra-benchmarks BENCH=benchmarks/terminal-bench" in result.output, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
