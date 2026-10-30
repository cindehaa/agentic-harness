"""run_trial end to end on bash-operations with scripted fake agents (real Docker, no model)."""

import json
import subprocess
import tarfile
import time
from collections.abc import Callable
from dataclasses import replace

import pytest
from litellm.exceptions import AuthenticationError

from agentic_harness.agents import Agent, AgentLimitation, AgentSession, AgentStats
from agentic_harness.benchmarks import BashOperations, GraderError
from agentic_harness.environments import DockerEnvironment, Environment
from agentic_harness.runner import run_trial
from agentic_harness.types import Limits, Message, Score, Text, score_to_float

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)

bench = BashOperations()
TASK = next(t for t in bench.tasks() if t.id == "bash-001")


@pytest.fixture(scope="module", autouse=True)
def image():
    bench.setup([TASK])


class ScriptedAgent(Agent):
    """A fake agent: on each message it runs `act(env)`, then replies "done"."""

    name = "scripted"

    def __init__(self, act: Callable[[Environment], None]) -> None:
        self.act = act

    def start(self, env, artifacts_dir, limits, mcp_servers=()) -> AgentSession:
        agent = self

        class Session(AgentSession):
            replied = False

            def send(self, message: Message) -> Message:
                agent.act(env)
                self.replied = True
                return Message("agent", (Text("done"),))

            def stats(self) -> AgentStats:
                return AgentStats("done", 0.01) if self.replied else AgentStats()

        return Session()


def kill_container(env) -> None:
    env.exec("kill 1")  # the init forwards SIGTERM to the main sleep; the container exits
    while env.state().running:  # wait until Docker has noticed, so the runner sees it as the agent's doing
        time.sleep(0.1)


class BrokenGrader(BashOperations):
    def grade(self, task, grader, env, transcript):
        raise GraderError("grader crashed")


class MissingGraderImage(BashOperations):
    def grader_image(self, task):
        return "harness-no-such-image:nope"


class FixedGrader(BashOperations):
    def __init__(self, value) -> None:
        self.value = value

    def grade(self, task, grader, env, transcript):
        return Score(self.value)


def test_oracle_agent_is_graded_true(tmp_path):
    outcome = run_trial(bench, ScriptedAgent(lambda env: env.exec("echo 'Hello World' > hello.txt")), TASK, Limits(), tmp_path, DockerEnvironment)
    assert (outcome.status, outcome.score.value, outcome.score_float) == ("graded", True, 1.0)
    assert (outcome.agent_exit_status, outcome.cost_usd, outcome.turns) == ("done", 0.01, 1)
    saved = json.loads((tmp_path / "outcome.json").read_text())
    assert saved["status"] == "graded" and saved["score"]["value"] is True
    transcript = json.loads((tmp_path / "transcript.json").read_text())
    assert [m["role"] for m in transcript] == ["user", "agent"]
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [(e["seq"], e["kind"], e["turn"]) for e in events] == [
        (0, "message_sent", 1), (1, "command", 1), (2, "message_received", 1), (3, "workspace_saved", 1)]
    command = events[1]["payload"]
    assert (command["command"], command["exit_code"], command["truncated"]) == ("echo 'Hello World' > hello.txt", 0, False)
    with tarfile.open(tmp_path / "workspace.tar") as tar:  # the agent's end state, saved before grading
        assert b"Hello World" in tar.extractfile("./hello.txt").read()


def test_empty_agent_is_graded_false(tmp_path):
    outcome = run_trial(bench, ScriptedAgent(lambda env: None), TASK, Limits(), tmp_path, DockerEnvironment)
    assert (outcome.status, outcome.score.value, outcome.score_float) == ("graded", False, 0.0)


def test_agent_that_kills_the_container_is_graded_false(tmp_path):
    outcome = run_trial(bench, ScriptedAgent(kill_container), TASK, Limits(), tmp_path, DockerEnvironment)
    assert (outcome.status, outcome.score.value) == ("graded", False)
    assert outcome.score.explanation == "container died during agent turn"


@pytest.mark.parametrize("docker_notices_late", [False, True])
def test_agent_that_kills_the_container_without_waiting_is_graded_false(tmp_path, monkeypatch, docker_notices_late):
    # the race: Docker may notice the exit only after the post-turn check, i.e. during the pre-grade kill or grading
    if docker_notices_late:
        real_state, checks = DockerEnvironment.state, []

        def state(self):  # the first check after the turn still sees it running
            checks.append(1)
            return replace(real_state(self), running=True) if len(checks) == 1 else real_state(self)

        monkeypatch.setattr(DockerEnvironment, "state", state)
    outcome = run_trial(bench, ScriptedAgent(lambda env: env.exec("kill 1")), TASK, Limits(), tmp_path, DockerEnvironment)
    assert (outcome.status, outcome.score.value) == ("graded", False)
    assert outcome.score.explanation == "container died during agent turn"


def test_agent_limitation_grades_the_world_as_it_is(tmp_path):
    def act(env):
        env.exec("echo 'Hello World' > hello.txt")
        raise AgentLimitation("context window exceeded")

    outcome = run_trial(bench, ScriptedAgent(act), TASK, Limits(), tmp_path, DockerEnvironment)
    assert (outcome.status, outcome.score.value) == ("graded", True)
    assert outcome.agent_exit_status == "AgentLimitation: context window exceeded"


def test_provider_auth_error_is_infra_error_with_traceback(tmp_path):
    def act(env):
        raise AuthenticationError("invalid x-api-key", llm_provider="anthropic", model="fake")

    outcome = run_trial(bench, ScriptedAgent(act), TASK, Limits(), tmp_path, DockerEnvironment)
    assert outcome.status == "infra_error"
    assert "Traceback" in outcome.error and "AuthenticationError" in outcome.error
    assert not outcome.retryable  # another attempt would fail the same way


@pytest.mark.parametrize("value", [{"passed": True}, object()])  # no float conversion; not JSON
def test_unusable_score_value_is_grader_error(tmp_path, value):
    outcome = run_trial(FixedGrader(value), ScriptedAgent(lambda env: None), TASK, Limits(), tmp_path, DockerEnvironment)
    assert outcome.status == "grader_error" and "unusable score value" in outcome.error


def test_events_are_appended_as_they_happen_and_everything_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_harness.redact._secrets", lambda: ("Hello World",))  # pretend it's a secret
    turns_ended = []
    outcome = run_trial(FixedGrader(True), ScriptedAgent(lambda env: None), TASK, Limits(), tmp_path, DockerEnvironment,
                        on_turn_end=lambda: turns_ended.append(1))
    assert outcome.status == "graded" and turns_ended == [1]
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [(e["seq"], e["kind"], e["turn"]) for e in events] == [
        (0, "message_sent", 1), (1, "message_received", 1), (2, "workspace_saved", 1)]
    for name in ("events.jsonl", "transcript.json", "outcome.json"):
        text = (tmp_path / name).read_text()
        assert "Hello World" not in text
    assert "***" in (tmp_path / "transcript.json").read_text()
    assert not list(tmp_path.glob("*.tmp"))


def test_grader_crash_is_grader_error(tmp_path):
    outcome = run_trial(BrokenGrader(), ScriptedAgent(lambda env: None), TASK, Limits(), tmp_path, DockerEnvironment)
    assert (outcome.status, outcome.error, outcome.score) == ("grader_error", "grader crashed", None)
    assert json.loads((tmp_path / "outcome.json").read_text())["status"] == "grader_error"


def test_grader_that_cannot_start_is_infra_error(tmp_path):
    outcome = run_trial(MissingGraderImage(), ScriptedAgent(lambda env: None), TASK, Limits(), tmp_path, DockerEnvironment)
    assert (outcome.status, outcome.retryable) == ("infra_error", True) and "DockerError" in outcome.error


def test_grader_and_workspace_volume_are_removed_after_the_trial(tmp_path):
    made = []

    def factory(image, lifetime_s, limits):
        made.append(DockerEnvironment(image, lifetime_s, limits))
        return made[-1]

    outcome = run_trial(bench, ScriptedAgent(lambda env: None), TASK, Limits(), tmp_path, factory)
    [env] = made
    [grader] = env.attached
    assert outcome.status == "graded" and grader.image == "python:3.11-slim"
    assert env.container_id is None and grader.container_id is None and env.volume is None
    assert _labelled("ps", "-a") == "" and _labelled("volume", "ls") == ""


def _labelled(*cmd: str) -> str:
    return subprocess.run(["docker", *cmd, "-q", "--filter", "label=agentic-harness=1"], capture_output=True, text=True).stdout


def test_leftover_agent_process_is_killed_before_grading(tmp_path):
    # a background writer would overwrite hello.txt after the agent "finished"; it must be dead before grading
    act = lambda env: env.exec(
        "(sleep 1; while true; do echo wrong > hello.txt; sleep 0.1; done) > /dev/null 2>&1 & echo 'Hello World' > hello.txt"
    )
    outcome = run_trial(bench, ScriptedAgent(act), TASK, Limits(), tmp_path, DockerEnvironment)
    assert outcome.score.value is True


def test_missing_image_is_infra_error(tmp_path):
    task = type(TASK)(**{**TASK.__dict__, "image": "harness-no-such-image:nope"})
    outcome = run_trial(bench, ScriptedAgent(lambda env: None), task, Limits(), tmp_path, DockerEnvironment)
    assert outcome.status == "infra_error" and "DockerError" in outcome.error
    assert (tmp_path / "outcome.json").exists()


@pytest.mark.parametrize(
    "value, expected", [(True, 1.0), ("C", 1.0), ("P", 0.5), (False, 0.0), ("I", 0.0), ("N", 0.0), (0.25, 0.25), (3, 3.0)]
)
def test_score_to_float(value, expected):
    assert score_to_float(value) == expected


def test_score_to_float_rejects_other_values():
    with pytest.raises(ValueError):
        score_to_float({"passed": True})


def test_no_leftover_containers_or_volumes():
    assert _labelled("ps", "-a") == "" and _labelled("volume", "ls") == ""
