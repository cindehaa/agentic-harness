"""run_trial's rules against a fake Environment: no Docker, no model. The runner only knows the Environment port,
so any backend (here an in-memory fake) can stand in for Docker."""

from collections.abc import Callable

from agentic_harness.agents import Agent, AgentSession
from agentic_harness.benchmarks import Benchmark
from agentic_harness.environments import Environment, EnvironmentLost, EnvironmentState, ExecResult
from agentic_harness.runner import run_trial
from agentic_harness.types import Limits, Message, Score, Task, Text

TASK = Task("fake", "t1", "fake:image")


class FakeEnvironment(Environment):
    """A world that is a dict of files. `state_now` is what state() reports; `lose_on_exec` makes exec raise."""

    def __init__(self, image: str, lifetime_s: int, limits: Limits) -> None:
        super().__init__(image)
        self.lifetime_s, self.limits = lifetime_s, limits
        self.files: dict[str, bytes] = {}
        self.state_now: EnvironmentState | None = EnvironmentState(running=True, exit_code=0, oom_killed=False)
        self.lose_on_exec = False
        self.started = self.stopped = self.killed = False

    def start(self): self.started = True
    def stop(self): self.stopped = True
    def state(self): return self.state_now
    def kill_agent_processes(self): self.killed = True
    def write_file(self, path, content): self.files[path] = content.encode() if isinstance(content, str) else content
    def read_file(self, path): return self.files[path]
    def upload(self, local, remote): ...
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

    def grade(self, task, env, transcript):
        return Score(env.files.get("answer.txt") == b"ok")


class ScriptedAgent(Agent):
    name = "scripted"

    def __init__(self, act: Callable[[FakeEnvironment], None]) -> None:
        self.act = act

    def start(self, env, artifacts_dir, limits) -> AgentSession:
        act = self.act

        class Session(AgentSession):
            def send(self, message: Message) -> Message:
                act(env)
                return Message("agent", (Text("done"),))

        return Session()


def write_ok(env: FakeEnvironment) -> None:
    env.write_file("answer.txt", "ok")


def die(**state) -> Callable[[FakeEnvironment], None]:
    def act(env: FakeEnvironment) -> None:
        env.state_now = EnvironmentState(**state) if state else None

    return act


def run(act, tmp_path, limits=Limits()):
    factory = Factory()
    outcome = run_trial(OneMessage(), ScriptedAgent(act), TASK, limits, tmp_path, factory)
    return outcome, factory.env


def test_runner_uses_the_injected_factory_and_always_stops(tmp_path):
    outcome, env = run(write_ok, tmp_path)
    assert (outcome.status, outcome.score.value, outcome.score_float) == ("graded", True, 1.0)
    assert env.started and env.killed and env.stopped and env.image == "fake:image"


def test_main_process_killed_during_turn_is_graded_fail(tmp_path):
    outcome, env = run(die(running=False, exit_code=137, oom_killed=False), tmp_path)
    assert (outcome.status, outcome.score.value, outcome.score.explanation) == ("graded", False, "container died during agent turn")
    assert not env.killed  # nothing graded: the world is gone


def test_vanished_container_is_infra_error(tmp_path):
    outcome, _ = run(die(), tmp_path)
    assert outcome.status == "infra_error" and "container lost" in outcome.error


def test_environment_lost_while_still_running_is_infra_error(tmp_path):
    def act(env):
        env.lose_on_exec = True
        env.exec("true")

    outcome, env = run(act, tmp_path)
    assert (outcome.status, outcome.error) == ("infra_error", "EnvironmentLost: backend unreachable") and env.stopped


def test_keep_alive_ending_normally_is_infra_error(tmp_path):
    # exit code 0: the keep-alive's lifetime ran out, the harness sized it too short; not the agent's doing
    outcome, _ = run(die(running=False, exit_code=0, oom_killed=False), tmp_path)
    assert (outcome.status, outcome.error) == ("infra_error", "container lifetime expired")


def test_container_lifetime_is_several_times_the_trial_timeout(tmp_path):
    _, env = run(write_ok, tmp_path, Limits(trial_timeout_s=100))
    assert env.lifetime_s >= 3 * 100


def test_out_of_memory_under_our_limit_is_graded_fail(tmp_path):
    outcome, _ = run(die(running=False, exit_code=137, oom_killed=True), tmp_path)
    assert (outcome.status, outcome.score.value) == ("graded", False)
    assert outcome.score.explanation == "container ran out of memory during agent turn"


def test_limits_reach_the_environment(tmp_path):
    _, env = run(write_ok, tmp_path, Limits(memory="1g", cpus=0.5))
    assert (env.limits.memory, env.limits.cpus) == ("1g", 0.5)
