"""run_trial's rules against a fake Environment: no Docker, no model. The runner only knows the Environment port,
so any backend (here an in-memory fake) can stand in for Docker."""

import json
from collections.abc import Callable

import pytest

from agentic_harness.agents import Agent, AgentCapabilities, AgentSession, TransientError
from agentic_harness.benchmarks import Benchmark
from agentic_harness.environments import EndReason, Environment, EnvironmentLost, EnvironmentState, ExecResult
from agentic_harness.runner import run_trial
from agentic_harness.types import Limits, Message, Score, Task, Text

TASK = Task("fake", "t1", "fake:image")


class FakeEnvironment(Environment):
    """A world that is a dict of files. `state_now` is what state() reports; `lose_on_exec` makes exec raise."""

    def __init__(self, image: str, lifetime_s: int, limits: Limits) -> None:
        super().__init__(image)
        self.lifetime_s, self.limits = lifetime_s, limits
        self.files: dict[str, bytes] = {}
        self.state_now: EnvironmentState | None = EnvironmentState(running=True)
        self.lose_on_exec = False
        self.started = self.stopped = self.killed = False
        self.graders: list[FakeEnvironment] = []

    def start(self):
        if self.image == "missing:image":
            raise EnvironmentLost("no such image")
        self.started = True
    def stop(self): self.stopped = True
    def state(self): return self.state_now
    def kill_agent_processes(self): self.killed = True

    def save_workspace(self, local, max_bytes):
        if "answer.txt" not in self.files:
            raise OSError("tar failed: no workspace")
        local.write_bytes(b"tar of " + self.files["answer.txt"])
        return False
    def write_file(self, path, content): self.files[path] = content.encode() if isinstance(content, str) else content
    def read_file(self, path): return self.files[path]
    def upload(self, local, remote): ...

    def attach_grader(self, image):
        grader = FakeEnvironment(image, self.lifetime_s, self.limits)
        grader.files = self.files  # the shared workspace
        self.graders.append(grader)
        return grader
    def download(self, remote, local): ...

    def exec(self, cmd, timeout=60, cwd=None) -> ExecResult:
        if self.lose_on_exec:
            raise EnvironmentLost("backend unreachable")
        return ExecResult(0, "", "")


class Factory:
    """An EnvironmentFactory that remembers the one environment it made."""

    def __call__(self, image: str, lifetime_s: int, limits: Limits) -> FakeEnvironment:
        self.env = FakeEnvironment(image, lifetime_s, limits)
        return self.env


class OneMessage(Benchmark[Task]):
    """Sends one message; passes if the agent wrote "ok" to answer.txt."""

    name = "one-message"

    def tasks(self): return [TASK]
    def run_agent(self, task, agent, env): agent.send(Message("user", (Text("write ok"),)))

    def grade(self, task, grader, env, transcript):
        return Score(grader.files.get("answer.txt") == b"ok")


class ScriptedAgent(Agent):
    name = "scripted"

    def __init__(self, act: Callable[[Environment], None]) -> None:
        self.act = act

    def start(self, env, artifacts_dir, limits) -> AgentSession:
        act = self.act

        class Session(AgentSession):
            def send(self, message: Message) -> Message:
                act(env)
                return Message("agent", (Text("done"),))

        return Session()


def write_ok(env: Environment) -> None:
    env.write_file("answer.txt", "ok")


def fake(env: Environment) -> FakeEnvironment:
    return env.env  # the agent is handed a RecordingEnvironment; the fake is behind it


def die(reason: EndReason | None = None) -> Callable[[Environment], None]:
    """The world ends during the agent's turn for `reason`; None: it vanishes (state() is None)."""

    def act(env: Environment) -> None:
        fake(env).state_now = EnvironmentState(running=False, end_reason=reason) if reason else None

    return act


def run(act, tmp_path, limits=Limits()):
    factory = Factory()
    outcome = run_trial(OneMessage(), ScriptedAgent(act), TASK, limits, tmp_path, factory)
    return outcome, factory.env


def test_runner_uses_the_injected_factory_and_always_stops(tmp_path):
    outcome, env = run(write_ok, tmp_path)
    assert (outcome.status, outcome.score.value, outcome.score_float) == ("graded", True, 1.0)
    assert env.started and env.killed and env.stopped and env.image == "fake:image"


def test_grading_runs_in_an_attached_grader_that_is_stopped(tmp_path):
    outcome, env = run(write_ok, tmp_path)
    [grader] = env.graders
    assert outcome.score.value is True  # the grader saw the trial's workspace
    assert grader.image == "fake:image" and grader.started and grader.stopped  # default grader_image: the task's
    assert (grader.lifetime_s, grader.limits) == (env.lifetime_s, env.limits)


def test_main_process_killed_during_turn_is_graded_fail(tmp_path):
    outcome, env = run(die(EndReason.KILLED), tmp_path)
    assert (outcome.status, outcome.score.value, outcome.score.explanation) == ("graded", False, "container died during agent turn")
    assert not env.killed  # nothing graded: the world is gone


def test_vanished_container_is_infra_error(tmp_path):
    outcome, _ = run(die(), tmp_path)
    assert outcome.status == "infra_error" and "container lost" in outcome.error


def test_environment_lost_while_still_running_is_infra_error(tmp_path):
    def act(env):
        fake(env).lose_on_exec = True
        env.exec("true")

    outcome, env = run(act, tmp_path)
    assert (outcome.status, outcome.error) == ("infra_error", "EnvironmentLost: backend unreachable") and env.stopped


def test_keep_alive_ending_normally_is_infra_error(tmp_path):
    # the keep-alive's lifetime ran out, the harness sized it too short; not the agent's doing
    outcome, _ = run(die(EndReason.EXPIRED), tmp_path)
    assert (outcome.status, outcome.error) == ("infra_error", "container lifetime expired")


def test_container_lifetime_is_several_times_the_trial_timeout(tmp_path):
    _, env = run(write_ok, tmp_path, Limits(trial_timeout_s=100))
    assert env.lifetime_s >= 3 * 100


def test_out_of_memory_under_our_limit_is_graded_fail(tmp_path):
    outcome, _ = run(die(EndReason.RESOURCE_LIMIT), tmp_path)
    assert (outcome.status, outcome.score.value) == ("graded", False)
    assert outcome.score.explanation == "container exceeded its resource limits during agent turn"
    assert outcome.score.metadata == {"end_reason": "resource_limit"}  # JSON, for the attempts row


def test_world_that_ended_for_an_unknown_reason_is_infra_error(tmp_path):
    outcome, _ = run(die(EndReason.UNKNOWN), tmp_path)
    assert (outcome.status, outcome.retryable, outcome.error) == ("infra_error", True, "container ended for an unknown reason")


def test_limits_reach_the_environment(tmp_path):
    _, env = run(write_ok, tmp_path, Limits(memory="1g", cpus=0.5))
    assert (env.limits.memory, env.limits.cpus) == ("1g", 0.5)


def raises(error: Exception) -> Callable[[Environment], None]:
    def act(env: Environment) -> None:
        raise error

    return act


@pytest.mark.parametrize("error, retryable", [
    (TransientError("provider overloaded"), True),
    (ConnectionError("network down"), True),
    (EnvironmentLost("backend unreachable"), True),
    (PermissionError("403 from the provider"), False),  # e.g. auth: another attempt fails the same way
    (KeyError("a bug in an adapter"), False),
])
def test_which_infra_errors_are_retryable(tmp_path, error, retryable):
    outcome, _ = run(raises(error), tmp_path)
    assert (outcome.status, outcome.retryable) == ("infra_error", retryable)


def test_grader_that_cannot_start_and_lost_container_are_retryable(tmp_path):
    class MissingGrader(OneMessage):
        def grader_image(self, task):
            return "missing:image"

    outcome = run_trial(MissingGrader(), ScriptedAgent(write_ok), TASK, Limits(), tmp_path, Factory())
    assert (outcome.status, outcome.retryable, outcome.error) == ("infra_error", True, "EnvironmentLost: no such image")
    outcome, _ = run(die(), tmp_path)
    assert (outcome.status, outcome.retryable) == ("infra_error", True)


def test_agent_commands_are_traced_by_turn_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_harness.redact._secrets", lambda: ("sk-secret-value",))

    def act(env):
        env.exec("echo sk-secret-value")

    run(act, tmp_path)
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [(e["seq"], e["turn"], e["kind"]) for e in events] == [
        (0, 1, "message_sent"), (1, 1, "command"), (2, 1, "message_received"), (3, 1, "workspace_saved")]
    command = events[1]["payload"]
    assert command["command"] == "echo ***" and command["exit_code"] == 0 and command["duration_s"] >= 0


def test_harness_decided_failure_scores_0_without_asking_the_benchmark(tmp_path):
    class OddScale(OneMessage):
        def score_to_float(self, score):
            raise AssertionError("not for a failure the harness decided")

    outcome = run_trial(OddScale(), ScriptedAgent(die(EndReason.KILLED)), TASK, Limits(), tmp_path, Factory())
    assert (outcome.status, outcome.score_float) == ("graded", 0.0)


def test_final_workspace_is_saved_before_grading_and_noted_in_the_trace(tmp_path):
    outcome, _ = run(write_ok, tmp_path)
    assert outcome.status == "graded" and (tmp_path / "workspace.tar").read_bytes() == b"tar of ok"
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == "workspace_saved" and events[-1]["payload"] == {"file": "workspace.tar", "bytes": 9, "truncated": False}


def test_unreadable_workspace_is_noted_not_fatal(tmp_path):
    outcome, _ = run(lambda env: None, tmp_path)
    assert (outcome.status, outcome.score.value) == ("graded", False)
    last = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert last["payload"] == {"error": "tar failed: no workspace"}


def test_secrets_in_the_saved_workspace_are_masked_and_the_tar_keeps_its_size(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_harness.redact._secrets", lambda: ("ok",))
    run(write_ok, tmp_path)
    assert (tmp_path / "workspace.tar").read_bytes() == b"tar of **"


class NeedsFilesAndMcp(OneMessage):
    def requirements(self, task):
        return AgentCapabilities(content_types=frozenset({"text", "file"}), multi_turn=True, mcp=True)


def test_agent_lacking_a_capability_is_unsupported_before_anything_starts(tmp_path):
    factory = Factory()
    outcome = run_trial(NeedsFilesAndMcp(), ScriptedAgent(write_ok), TASK, Limits(), tmp_path, factory)
    assert outcome.status == "unsupported"
    assert outcome.error == "scripted can't do what this task needs: content: file, multi_turn, mcp"
    assert not hasattr(factory, "env")  # no world was started
    assert json.loads((tmp_path / "outcome.json").read_text())["status"] == "unsupported"


def test_capabilities_cover_what_they_declare():
    agent = AgentCapabilities(content_types=frozenset({"text", "file"}), multi_turn=True, mcp=True)
    assert agent.missing(AgentCapabilities(content_types=frozenset({"file"}), multi_turn=True, mcp=True)) == []
    assert AgentCapabilities().missing(AgentCapabilities(content_types=frozenset({"data"}))) == ["content: data"]
