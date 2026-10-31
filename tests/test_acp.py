import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_harness.agents import AgentLimitation, PreflightError, TransientError, Unsupported, resolve_agent
from agentic_harness.agents.acp import (
    AcpAgent, AcpAgentError, AcpDefinition, built_in_definitions, derived_tag, dockerfile, load_definition,
)
from agentic_harness.environments import Environment, EnvironmentLost, EnvironmentState
from agentic_harness.environments import docker as docker_module
from agentic_harness.types import File, Limits, McpServer, Message, Text

FAKE = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"
LIVE = pytest.mark.skipif(not (os.getenv("RUN_LIVE") == "1" and os.getenv("ANTHROPIC_API_KEY")), reason="needs RUN_LIVE=1 and an API key")
DEFINITION = AcpDefinition(
    name="fake", version="1", command=("fake-agent",), install={"any": {"from": "fake/agent:1"}}, install_dir="/opt/fake",
    forward_env=("FAKE_API_KEY",), env={"FIXED": "1"}, model_env="FAKE_MODEL", model_prefix="anthropic/",
)


class HostWorld(Environment):
    def __init__(self, *args: str) -> None:
        super().__init__("fake:image", "/work")
        self.args = args
        self.killed = False
        self.running = True
        self.started: list[tuple] = []

    def start_process(self, argv, env=None, user=None):
        self.started.append((argv, env))
        return subprocess.Popen([sys.executable, str(FAKE), *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

    def kill_agent_processes(self): self.killed = True
    def state(self): return EnvironmentState(running=self.running)
    def exec(self, cmd, timeout=60, cwd=None, env=None, user=None): ...
    def read_file(self, path): ...
    def write_file(self, path, content): ...
    def start(self): ...
    def stop(self): ...
    def save_workspace(self, local, max_bytes): ...
    def attach(self, sidecar): ...
    def upload(self, local, remote): ...
    def download(self, remote, local): ...


def start(tmp_path, monkeypatch, world=None, limits=Limits(), servers=()):
    monkeypatch.setenv("FAKE_API_KEY", "sk-fake-123456789")
    world = world or HostWorld()
    return world, AcpAgent(DEFINITION, "anthropic/claude-haiku-5-5").start(world, tmp_path, limits, mcp_servers=servers)


def say(session, words: str) -> str:
    return session.send(Message("user", (Text(words),))).content[0].text


def test_a_method_we_dont_offer_gets_method_not_found(tmp_path, monkeypatch):
    _, session = start(tmp_path, monkeypatch)
    assert json.loads(say(session, "files"))["code"] == -32601


def test_setup_passes_the_worlds_workdir_the_mcp_servers_and_the_agents_variables(tmp_path, monkeypatch):
    server = McpServer("fixture", "img", ("python", "/server.py"), 8000)
    world, session = start(tmp_path, monkeypatch, servers=(server,))
    assert json.loads(say(session, "setup")) == {
        "cwd": "/work", "mcpServers": [{"type": "http", "name": "fixture", "url": server.url, "headers": []}]}
    argv, env = world.started[0]
    assert argv == ["fake-agent"]
    assert env == {"FAKE_API_KEY": "sk-fake-123456789", "FIXED": "1", "FAKE_MODEL": "claude-haiku-5-5"}


def test_limits_the_agent_keeps_itself_go_in_session_new_meta(tmp_path, monkeypatch):
    agent = AcpAgent(replace(DEFINITION, limits_meta={"max_steps": "fake.options.maxTurns"}), "anthropic/claude-haiku-5-5")
    monkeypatch.setenv("FAKE_API_KEY", "sk-fake-123456789")
    session = agent.start(HostWorld(), tmp_path, Limits(max_steps=7))
    assert json.loads(say(session, "setup"))["_meta"] == {"fake": {"options": {"maxTurns": 7}}}
    assert agent.unenforced_limits() == {"command_timeout_s"}


def test_without_limits_meta_the_step_limit_is_not_enforced_and_nothing_is_sent(tmp_path, monkeypatch):
    _, session = start(tmp_path, monkeypatch)
    assert "_meta" not in json.loads(say(session, "setup"))
    assert AcpAgent(DEFINITION, "anthropic/claude-haiku-5-5").unenforced_limits() == {"max_steps", "command_timeout_s"}


def test_limits_meta_must_name_a_limit():
    with pytest.raises(ValueError, match="names no limit"):
        AcpDefinition.from_dict("x", {"version": "1", "command": ["a"], "install": {}, "install_dir": "/a",
                                      "limits_meta": {"max_stepz": "a.b"}})


def test_a_definition_claiming_mcp_the_agent_lacks_fails_setup(tmp_path, monkeypatch):
    with pytest.raises(AcpAgentError, match="MCP servers over HTTP"):
        start(tmp_path, monkeypatch, world=HostWorld("nohttp"), servers=(McpServer("s", "img", (), 1),))


def test_a_turn_is_traced_and_replies_with_its_last_message(tmp_path, monkeypatch):
    _, session = start(tmp_path, monkeypatch)
    assert say(session, "work on it") == "done 1 on it"
    events = session.new_events()
    assert [k for k, _ in events] == ["session", "agent_text", "permission", "tool_call", "tool_result"]
    payloads = dict(events[1:])
    assert payloads["agent_text"] == {"text": "Let me look."}
    assert payloads["permission"] == {"tool_call": "t1", "granted": "allow_once"}
    assert payloads["tool_call"] == {"id": "t1", "name": "ls", "title": "ls", "kind": "execute", "input": {"command": "ls"}}
    assert payloads["tool_result"]["content"] == "a.txt (permission: once)" and not payloads["tool_result"]["is_error"]
    assert session.new_events() == []
    lines = (tmp_path / "acp.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["message"]["method"] == "initialize"


def test_turns_continue_one_session_and_stats_add_up(tmp_path, monkeypatch):
    _, session = start(tmp_path, monkeypatch)
    say(session, "work")
    assert say(session, "work again") == "done 2 again"
    stats = session.stats()
    assert stats.cost_usd == pytest.approx(0.02)
    assert (stats.input_tokens, stats.output_tokens, stats.exit_status) == (300, 40, "end_turn")


def test_deadline_cancels_the_turn(tmp_path, monkeypatch):
    world, session = start(tmp_path, monkeypatch, limits=Limits(trial_timeout_s=3))
    assert say(session, "hang") == ""
    assert session.stats().exit_status == "TimeLimit" and not world.killed


def test_an_agent_ignoring_the_cancel_is_killed(tmp_path, monkeypatch):
    import agentic_harness.agents.acp as acp

    monkeypatch.setattr(acp, "CANCEL_GRACE_S", 1)
    world, session = start(tmp_path, monkeypatch, limits=Limits(trial_timeout_s=2))
    assert say(session, "hang hard") == ""
    assert world.killed and session.stats().exit_status == "TimeLimit"
    assert say(session, "work") == "" and session.stats().exit_status == "TimeLimit"


def test_close_ends_the_agent_without_killing_the_world(tmp_path, monkeypatch):
    world, session = start(tmp_path, monkeypatch)
    session.close()
    assert session.proc.poll() is not None and not world.killed


def test_spending_past_the_budget_cancels_the_turn(tmp_path, monkeypatch):
    _, session = start(tmp_path, monkeypatch, limits=Limits(cost_usd=1.0))
    say(session, "spend")
    assert (session.stats().exit_status, session.stats().cost_usd) == ("LimitsExceeded", 5.0)
    assert say(session, "work") == "" and session.stats().exit_status == "LimitsExceeded"


def test_refusal_is_an_agent_limitation(tmp_path, monkeypatch):
    _, session = start(tmp_path, monkeypatch)
    with pytest.raises(AgentLimitation):
        say(session, "refuse")


@pytest.mark.parametrize("message, error", [
    ("API Error 529 overloaded_error", TransientError),
    ("rate_limit exceeded", TransientError),
    ("prompt is too long: 250000 tokens", AgentLimitation),
    ("invalid x-api-key", AcpAgentError),
])
def test_errors_are_classified_by_their_message(tmp_path, monkeypatch, message, error):
    _, session = start(tmp_path, monkeypatch)
    with pytest.raises(error):
        say(session, f"fail {message}")


def test_an_agent_failing_the_prompt_at_its_step_limit_ends_the_turn_like_one(tmp_path, monkeypatch):
    _, session = start(tmp_path, monkeypatch)
    assert say(session, "fail Internal error: Reached maximum number of turns (7)") == ""
    assert session.exit_status == "LimitsExceeded"


def test_an_agent_that_dies_is_an_agent_error_unless_the_world_died(tmp_path, monkeypatch):
    world, session = start(tmp_path, monkeypatch)
    with pytest.raises(AcpAgentError, match="ended"):
        say(session, "crash")
    world, session = start(tmp_path, monkeypatch)
    world.running = False
    with pytest.raises(EnvironmentLost):
        say(session, "crash")


def test_an_exception_in_our_handler_fails_the_turn_as_retryable(tmp_path, monkeypatch):
    _, session = start(tmp_path, monkeypatch)
    monkeypatch.setattr(session, "_tool", lambda u: 1 / 0)
    with pytest.raises(TransientError, match="ZeroDivisionError"):
        say(session, "work")


def test_non_text_content_is_unsupported(tmp_path, monkeypatch):
    _, session = start(tmp_path, monkeypatch)
    with pytest.raises(Unsupported):
        session.send(Message("user", (File("/workspace/a.png", "image/png"),)))


def test_claude_code_is_a_built_in_acp_agent():
    agent = resolve_agent("claude-code")("anthropic/claude-haiku-5-5")
    assert isinstance(agent, AcpAgent) and agent.name == "claude-code"
    assert agent.native_model() == "claude-haiku-5-5" and agent.model == "anthropic/claude-haiku-5-5"
    assert agent.capabilities.multi_turn and agent.capabilities.mcp and agent.capabilities.content_types == {"text"}
    assert agent.forward_env == ("ANTHROPIC_API_KEY",)
    assert agent.definition.limits_meta == {"max_steps": "claudeCode.options.maxTurns"}
    assert "claude-code" in built_in_definitions()


def test_a_private_agent_is_a_definition_file(tmp_path):
    path = tmp_path / "secret-agent.toml"
    path.write_text('version = "2"\ncommand = ["/opt/s/run", "--acp"]\ninstall_dir = "/opt/s"\n'
                    'install.any = { from = "registry.example/secret-agent:2" }\n')
    agent = resolve_agent(str(path))("openai/gpt-x")
    assert agent.name == str(path) and agent.definition == load_definition(path)
    assert agent.native_model() == "openai/gpt-x" and agent.capabilities.multi_turn and not agent.capabilities.mcp
    assert "COPY" not in dockerfile(agent.definition, "task:1")
    assert "--mount=from=acp-any,source=/opt/s,target=/mnt/any" in dockerfile(agent.definition, "task:1")


def test_unknown_agents_are_refused():
    with pytest.raises(ValueError, match="unknown agent"):
        resolve_agent("nope")


def test_the_image_recipe_copies_the_install_matching_the_worlds_libc():
    d = built_in_definitions()["claude-code"]
    text = dockerfile(d, "task:1")
    assert "FROM node:22-alpine AS acp-musl" in text and "FROM node:22-bookworm-slim AS acp-glibc" in text
    assert "FROM task:1\n" in text and "ld-musl" in text and "apk add" in text
    assert derived_tag(d, "sha256:aaa") != derived_tag(d, "sha256:bbb")
    assert derived_tag(d, "sha256:aaa").startswith("harness-acp-claude-code:")


def test_preflight_needs_the_agents_model_family_and_variables(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "x")
    AcpAgent(DEFINITION, "anthropic/claude-haiku-5-5").preflight()
    AcpAgent(DEFINITION, "claude-haiku-5-5").preflight()
    with pytest.raises(PreflightError, match="anthropic/"):
        AcpAgent(DEFINITION, "openai/gpt-x").preflight()
    monkeypatch.delenv("FAKE_API_KEY")
    with pytest.raises(PreflightError, match="FAKE_API_KEY"):
        AcpAgent(DEFINITION, "anthropic/claude-haiku-5-5").preflight()


@pytest.mark.skipif(subprocess.run(["docker", "info"], capture_output=True).returncode != 0, reason="Docker is not running")
def test_the_agent_runs_inside_the_world_over_docker_exec(tmp_path, monkeypatch):
    from agentic_harness.environments import DockerEnvironment, ensure_image

    ensure_image("python:3.11-slim")
    with DockerEnvironment("python:3.11-slim", lifetime_s=120) as world:
        world.upload(FAKE, "/opt/fake/agent.py")
        definition = AcpDefinition(name="fake", version="1", command=("python", "/opt/fake/agent.py"),
                                   install={"any": {"from": "x"}}, install_dir="/opt/fake", forward_env=("FAKE_API_KEY",))
        monkeypatch.setenv("FAKE_API_KEY", "sk-fake-123456789")
        session = AcpAgent(definition, "m").start(world, tmp_path, Limits())
        assert say(session, "work") == "done 1 "
        assert json.loads(say(session, "setup"))["cwd"] == "/workspace"
        world.kill_agent_processes()
        time.sleep(0.5)
        assert session.proc.poll() is not None
    out = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={docker_module.LABEL}"], capture_output=True, text=True).stdout
    assert out == ""


def _live_trial(bench, task, tmp_path):
    from agentic_harness.environments import DockerEnvironment
    from agentic_harness.runner import run_trial

    outcome = run_trial(bench, resolve_agent("claude-code")(os.environ["HARNESS_MODEL"]), task, Limits(), tmp_path, DockerEnvironment)
    print(f"live cost: ${outcome.cost_usd:.4f}, tokens {outcome.input_tokens}/{outcome.output_tokens}, exit: {outcome.agent_exit_status}")
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert os.environ["ANTHROPIC_API_KEY"] not in "".join(p.read_text() for p in tmp_path.glob("*.json*"))
    return outcome, events


@pytest.mark.live
@LIVE
def test_live_claude_code_solves_bash_001(tmp_path):
    from agentic_harness.benchmarks import BashOperations

    bench = BashOperations()
    task = next(t for t in bench.tasks() if t.id == "bash-001")
    bench.setup([task])
    outcome, events = _live_trial(bench, task, tmp_path)
    assert (outcome.status, outcome.score.value, outcome.agent_exit_status) == ("graded", True, "end_turn"), outcome.error
    assert {"tool_call", "tool_result", "permission"} <= {e["kind"] for e in events}
    assert outcome.cost_usd > 0 and outcome.input_tokens > 0


@pytest.mark.live
@LIVE
def test_live_claude_code_calls_a_tool_on_the_mcp_fixture_sidecar(tmp_path):
    from test_mcp import FIXTURE, SERVER, TASK, WithMcp

    from agentic_harness.environments import build_image, ensure_image
    from agentic_harness.types import Score

    build_image((FIXTURE / "Dockerfile").read_text(), context=FIXTURE, tag=SERVER.image)
    ensure_image(TASK.image)

    class SecretWord(WithMcp):
        def run_agent(self, task, agent, env):
            agent.send(Message("user", (Text("Get the secret word from the fixture server's tool and write it, "
                                             "and nothing else, to /workspace/word.txt"),)))

        def grade(self, task, grader, env, transcript):
            return Score(grader.read_file("/workspace/word.txt").strip() == b"pomegranate-42")

    outcome, events = _live_trial(SecretWord(), TASK, tmp_path)
    assert (outcome.status, outcome.score.value) == ("graded", True), outcome.error
    calls = [json.dumps(e["payload"]) for e in events if e["kind"] == "tool_call"]
    assert any("secret_word" in c for c in calls)


@pytest.mark.live
@LIVE
def test_live_claude_code_keeps_the_conversation_across_turns(tmp_path):
    from test_mcp import TASK, WithMcp

    from agentic_harness.environments import ensure_image
    from agentic_harness.types import Score

    ensure_image(TASK.image)

    class TwoTurns(WithMcp):
        def mcp_servers(self, task):
            return []

        def run_agent(self, task, agent, env):
            agent.send(Message("user", (Text("Remember the number 7. Reply with just OK; use no tools."),)))
            reply = agent.send(Message("user", (Text("Write the number I asked you to remember, and nothing else, "
                                                     "to /workspace/n.txt"),)))
            assert reply.content

        def grade(self, task, grader, env, transcript):
            return Score(grader.read_file("/workspace/n.txt").strip() == b"7")

    outcome, events = _live_trial(TwoTurns(), TASK, tmp_path)
    assert (outcome.status, outcome.score.value, outcome.turns) == ("graded", True, 2), outcome.error
    assert len({e["payload"]["session_id"] for e in events if e["kind"] == "session"}) == 1
