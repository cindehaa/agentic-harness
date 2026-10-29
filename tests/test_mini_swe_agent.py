"""MiniSweAgent: the env wrapper and multi-turn continuation against a fake env and mini's scripted model,
plus one live test against a real model and container (only with RUN_LIVE=1 and an API key)."""

import json
import os
import time

import pytest
from minisweagent.exceptions import LimitsExceeded, Submitted
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output

from agentic_harness.agents import MiniSweAgent, Unsupported, resolve_agent
from agentic_harness.agents.mini_swe_agent import ContainerEnv, MiniAgent, MiniSweSession, TimeLimit
from agentic_harness.environments import Environment, ExecResult
from agentic_harness.types import File, Limits, Message, Text

UNAME = "Linux\n6.10.14-linuxkit\n#1 SMP\naarch64\n"


class FakeEnv(Environment):
    """Answers uname, echoes `echo X` commands, and remembers every exec call."""

    def __init__(self) -> None:
        super().__init__("fake:image")
        self.calls: list[tuple] = []

    def exec(self, cmd, timeout=60, cwd=None) -> ExecResult:
        self.calls.append((cmd, timeout, cwd))
        if cmd.startswith("uname"):
            return ExecResult(0, UNAME, "")
        command = cmd.removeprefix("exec 2>&1\n")
        return ExecResult(0, command.removeprefix("echo ") + "\n", "")

    def start(self): ...
    def stop(self): ...
    def write_file(self, path, content): ...
    def read_file(self, path): ...
    def upload(self, local, remote): ...
    def download(self, remote, local): ...


def wrapper(deadline: float = float("inf")) -> tuple[FakeEnv, ContainerEnv]:
    env = FakeEnv()
    return env, ContainerEnv(env, deadline=deadline, command_timeout_s=60)


def test_template_vars_are_the_containers_os():
    _, cenv = wrapper()
    assert cenv.get_template_vars() == {"system": "Linux", "release": "6.10.14-linuxkit", "version": "#1 SMP", "machine": "aarch64"}


def test_execute_merges_streams_and_uses_default_cwd_and_timeout():
    env, cenv = wrapper()
    out = cenv.execute({"command": "echo hi"})
    assert out == {"output": "hi\n", "returncode": 0, "exception_info": ""}
    assert env.calls[-1] == ("exec 2>&1\necho hi", 60, None)  # None = the env's workdir
    cenv.execute({"command": "echo hi"}, cwd="/tmp", timeout=5)
    assert env.calls[-1] == ("exec 2>&1\necho hi", 5, "/tmp")


def test_submit_raises_submitted():
    _, cenv = wrapper()
    with pytest.raises(Submitted) as info:
        cenv.execute({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
    assert info.value.messages[0]["extra"] == {"exit_status": "Submitted", "submission": ""}


def test_command_after_deadline_raises_time_limit_without_running():
    env, cenv = wrapper(deadline=time.monotonic() - 1)
    calls_before = len(env.calls)
    with pytest.raises(TimeLimit) as info:
        cenv.execute({"command": "echo hi"})
    assert isinstance(info.value, LimitsExceeded)  # so mini's loop ends cleanly
    assert info.value.messages[0]["extra"]["exit_status"] == "TimeLimit"
    assert len(env.calls) == calls_before


def _bash(call_id: str, command: str) -> dict:
    tool_call = {"id": call_id, "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": command})}}
    return make_toolcall_output(None, [tool_call], [{"command": command, "tool_call_id": call_id}])


def scripted_session(outputs: list[dict], tmp_path) -> tuple[FakeEnv, MiniSweSession]:
    env, cenv = wrapper()
    model = DeterministicToolcallModel(outputs=outputs)
    agent = MiniAgent(model, cenv, system_template="sys", instance_template="{{task}}", cost_limit=0, output_path=tmp_path / "trajectory.json")
    return env, MiniSweSession(agent)


def test_second_send_continues_the_same_history(tmp_path):
    submit = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
    env, session = scripted_session([_bash("1", "echo a"), _bash("2", submit), _bash("3", "echo b"), _bash("4", submit)], tmp_path)
    assert session.send(Message("user", (Text("first"),))) == Message("agent", (Text(""),))
    assert (session.exit_status, session.steps) == ("Submitted", 2)
    session.send(Message("user", (Text("second"),)))
    roles = [m["role"] for m in session.agent.messages]
    # the submit's tool call got its result, then the new user message, then the agent went on
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool", "user", "assistant", "tool", "assistant", "exit"]
    assert session.agent.messages[6]["content"] == "second"
    assert (session.exit_status, session.steps) == ("Submitted", 4)
    assert json.loads((tmp_path / "trajectory.json").read_text())["info"]["exit_status"] == "Submitted"


def test_non_text_content_is_unsupported(tmp_path):
    _, session = scripted_session([], tmp_path)
    with pytest.raises(Unsupported):
        session.send(Message("user", (File("/workspace/a.png", "image/png"),)))


def test_resolve_agent_by_name_or_path():
    assert resolve_agent("mini-swe-agent") is MiniSweAgent
    assert resolve_agent("agents/mini-swe-agent") is MiniSweAgent
    with pytest.raises(ValueError):
        resolve_agent("nope")


@pytest.mark.live
@pytest.mark.skipif(not (os.getenv("RUN_LIVE") == "1" and os.getenv("ANTHROPIC_API_KEY")), reason="needs RUN_LIVE=1 and an API key")
def test_live_solves_bash_001(tmp_path):
    from agentic_harness.benchmarks import BashOperations
    from agentic_harness.environments import DockerEnvironment

    bench = BashOperations()
    task = next(t for t in bench.tasks() if t.id == "bash-001")
    bench.setup([task])
    with DockerEnvironment(task.image) as env:
        session = MiniSweAgent().start(env, tmp_path, Limits())
        bench.run_agent(task, session, env)
        score = bench.grade(task, env, ())
    print(f"live cost: ${session.cost_usd:.4f}, steps: {session.steps}, exit: {session.exit_status}")
    assert session.exit_status == "Submitted"
    assert score.value is True, score.explanation
    assert (tmp_path / "trajectory.json").exists()
