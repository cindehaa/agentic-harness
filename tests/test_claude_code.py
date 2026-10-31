"""ClaudeCode: stream-json parsing, MCP config, capabilities and the derived image's tag without a model; plus live
tests (only with RUN_LIVE=1 and an API key): bash-001, and a tool call on the MCP test-fixture sidecar."""

import json
import os
import subprocess

import pytest

from agentic_harness.agents import (
    AgentCapabilities, AgentLimitation, AgentStats, PreflightError, TransientError, Unsupported, resolve_agent,
)
from agentic_harness.agents.claude_code import (
    MCP_CONFIG, ClaudeCode, ClaudeCodeError, check_result, derived_tag, estimate_cost, mcp_config, parse_stream, trace_events,
    usage,
)
from agentic_harness.environments import Environment, ExecResult
from agentic_harness.types import File, Limits, McpServer, Message, Text

LIVE = pytest.mark.skipif(not (os.getenv("RUN_LIVE") == "1" and os.getenv("ANTHROPIC_API_KEY")), reason="needs RUN_LIVE=1 and an API key")

TOOL_USE = {"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Writing it."},
    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi > a.txt"}},
]}}
TOOL_RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "", "is_error": False}]}}
MCP_RESULT = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "pomegranate-42"}]},
]}}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "Done.", "session_id": "s1",
          "total_cost_usd": 0.002, "num_turns": 2, "stop_reason": "end_turn"}


def api_error(kind: str, status: int) -> list[dict]:
    """What Claude Code prints when an API call fails: a synthetic assistant message, then an error result."""
    return [{"type": "assistant", "error": kind, "message": {"content": [{"type": "text", "text": kind}]}},
            {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
             "api_error_status": status, "result": kind, "session_id": "s1", "total_cost_usd": 0, "num_turns": 1}]


def test_stream_lines_are_parsed_and_a_cut_off_line_is_skipped():
    text = json.dumps(TOOL_USE) + "\n" + json.dumps(RESULT) + '\n{"type": "assis'
    assert parse_stream(text) == [TOOL_USE, RESULT]


def test_tool_calls_and_results_become_trace_events():
    assert trace_events([TOOL_USE, TOOL_RESULT, MCP_RESULT, RESULT]) == [
        ("tool_call", {"id": "t1", "name": "Bash", "input": {"command": "echo hi > a.txt"}}),
        ("tool_result", {"id": "t1", "content": "", "truncated": False, "is_error": False}),
        ("tool_result", {"id": "t2", "content": "pomegranate-42", "truncated": False, "is_error": False}),
    ]


def test_long_tool_output_is_capped_in_the_trace():
    big = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "x" * 20_000}]}}
    [(_, payload)] = trace_events([big])
    assert len(payload["content"]) == 10_000 and payload["truncated"]


def test_result_event_is_returned():
    assert check_result([TOOL_USE, RESULT]) == RESULT


@pytest.mark.parametrize("kind, status, error", [
    ("authentication_failed", 401, ClaudeCodeError),  # infra_error, not retried
    ("model_not_found", 404, ClaudeCodeError),
    ("billing_error", 400, ClaudeCodeError),
    ("rate_limit", 429, TransientError),  # infra_error, retried
    ("overloaded", 529, TransientError),
    ("server_error", 500, TransientError),
    ("prompt_too_long", 400, AgentLimitation),  # graded as it is
    ("refusal", 400, AgentLimitation),
])
def test_api_errors_are_classified_like_minis(kind, status, error):
    with pytest.raises(error, match=kind):
        check_result(api_error(kind, status))


def test_refusal_stop_is_an_agent_limitation():
    with pytest.raises(AgentLimitation):
        check_result([RESULT | {"stop_reason": "refusal"}])


def test_no_result_event_is_claude_code_error():
    with pytest.raises(ClaudeCodeError, match="no result event"):
        check_result([TOOL_USE])


def test_mcp_config_names_each_server_by_its_url():
    servers = (McpServer("airline", "img", ("serve",), 8000), McpServer("user", "img2", ("serve",), 8001))
    assert json.loads(mcp_config(servers)) == {"mcpServers": {
        "airline": {"type": "http", "url": "http://localhost:8000/mcp"},
        "user": {"type": "http", "url": "http://localhost:8001/mcp"},
    }}


def test_capabilities_text_multi_turn_and_mcp():
    assert ClaudeCode.capabilities == AgentCapabilities(content_types=frozenset({"text"}), multi_turn=True, mcp=True)
    assert resolve_agent("claude-code") is ClaudeCode


def test_derived_tag_follows_the_base_image():
    assert derived_tag("sha256:aaa") == derived_tag("sha256:aaa")
    assert derived_tag("sha256:aaa") != derived_tag("sha256:bbb")
    assert derived_tag("sha256:aaa").startswith("harness-claude-code:")


def test_preflight_needs_an_anthropic_model_and_its_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    ClaudeCode("anthropic/claude-haiku-5-5").preflight()
    ClaudeCode("claude-haiku-5-5").preflight()
    with pytest.raises(PreflightError, match="Anthropic models only"):
        ClaudeCode("openai/gpt-x").preflight()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(PreflightError, match="ANTHROPIC_API_KEY"):
        ClaudeCode("anthropic/claude-haiku-5-5").preflight()


class FakeWorld(Environment):
    """Plays back one stream per `claude` run (written to the file it was told to write) and remembers calls."""

    def __init__(self, streams: list[list[dict]], timed_out: bool = False) -> None:
        super().__init__("fake:image")
        self.streams, self.timed_out = streams, timed_out
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple] = []
        self.killed = False

    def exec(self, cmd, timeout=60, cwd=None, env=None, user=None) -> ExecResult:
        self.calls.append((cmd, timeout, env))
        out = cmd[4]  # sh -c '<script>' sh <out file> claude ...
        self.files[out] = "".join(json.dumps(e) + "\n" for e in self.streams.pop(0)).encode()
        return ExecResult(-1 if self.timed_out else 0, "", "", timed_out=self.timed_out)

    def read_file(self, path): return self.files[path]
    def write_file(self, path, content): self.files[path] = content.encode()
    def kill_agent_processes(self): self.killed = True
    def start(self): ...
    def stop(self): ...
    def state(self): ...
    def save_workspace(self, local, max_bytes): ...
    def attach(self, sidecar): ...
    def upload(self, local, remote): ...
    def download(self, remote, local): ...


def test_native_model_drops_the_litellm_provider_prefix():
    assert ClaudeCode("anthropic/claude-haiku-5-5").native_model() == "claude-haiku-5-5"
    assert ClaudeCode("claude-haiku-5-5").native_model() == "claude-haiku-5-5"
    assert ClaudeCode("anthropic/claude-haiku-5-5").model == "anthropic/claude-haiku-5-5"  # runs.model keeps the id


def session(world, tmp_path, monkeypatch, limits=Limits(), servers=()):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    return ClaudeCode("anthropic/claude-haiku-5-5").start(world, tmp_path, limits, mcp_servers=servers)


def test_turns_resume_the_session_within_what_is_left_of_the_limits(tmp_path, monkeypatch):
    world = FakeWorld([[TOOL_USE, RESULT], [RESULT | {"total_cost_usd": 0.005, "num_turns": 1, "result": "Again."}]])
    s = session(world, tmp_path, monkeypatch, Limits(max_steps=10, cost_usd=0.5))
    assert s.send(Message("user", (Text("do it"),))) == Message("agent", (Text("Done."),))
    assert s.new_events()[0][0] == "tool_call" and s.new_events() == []
    assert s.send(Message("user", (Text("-again"),))) == Message("agent", (Text("Again."),))
    first, second = world.calls[0][0], world.calls[1][0]
    assert "--resume" not in first and second[second.index("--resume") + 1] == "s1"
    assert first[first.index("--model") + 1] == "claude-haiku-5-5"
    assert second[second.index("--max-turns") + 1] == "8" and second[second.index("--max-budget-usd") + 1] == "0.4980"
    assert second[-2:] == ["--", "-again"]  # a message starting with "-" is never read as a flag
    assert world.calls[0][2] == {"ANTHROPIC_API_KEY": "sk-test-key", "IS_SANDBOX": "1", "DISABLE_AUTOUPDATER": "1"}
    assert s.stats() == AgentStats("success", 0.005, 3)  # cost is the session's running total
    assert len((tmp_path / "trajectory.jsonl").read_text().splitlines()) == 3


def test_mcp_servers_are_written_to_claude_codes_config(tmp_path, monkeypatch):
    world = FakeWorld([[RESULT]])
    s = session(world, tmp_path, monkeypatch, servers=(McpServer("fixture", "img", ("serve",), 8000),))
    s.send(Message("user", (Text("go"),)))
    argv = world.calls[0][0]
    assert argv[argv.index("--mcp-config") + 1] == MCP_CONFIG
    assert json.loads(world.files[MCP_CONFIG])["mcpServers"]["fixture"]["url"] == "http://localhost:8000/mcp"


def test_deadline_kills_claude_inside_and_keeps_what_it_did(tmp_path, monkeypatch):
    world = FakeWorld([[TOOL_USE]], timed_out=True)
    s = session(world, tmp_path, monkeypatch)
    assert s.send(Message("user", (Text("go"),))) == Message("agent", (Text(""),))
    assert world.killed and s.stats().exit_status == "TimeLimit"
    assert s.stats().cost_usd == 0  # nothing spent in this stream: the assistant message carries no usage
    assert [k for k, _ in s.new_events()] == ["tool_call"]
    assert world.calls[0][1] <= Limits().trial_timeout_s  # exec timeout = what is left of the trial


def test_no_turn_starts_past_the_deadline(tmp_path, monkeypatch):
    world = FakeWorld([])
    s = session(world, tmp_path, monkeypatch, Limits(trial_timeout_s=0))
    assert s.send(Message("user", (Text("go"),))) == Message("agent", (Text(""),))
    assert world.calls == [] and s.stats().exit_status == "TimeLimit"


def test_non_text_content_is_unsupported(tmp_path, monkeypatch):
    with pytest.raises(Unsupported):
        session(FakeWorld([]), tmp_path, monkeypatch).send(Message("user", (File("/workspace/a.png", "image/png"),)))


def _no_leftovers() -> bool:
    out = subprocess.run(["docker", "ps", "-aq", "--filter", "label=agentic-harness=1"], capture_output=True, text=True).stdout
    return out == ""


@pytest.mark.live
@LIVE
def test_live_solves_bash_001(tmp_path):
    from agentic_harness.benchmarks import BashOperations
    from agentic_harness.environments import DockerEnvironment
    from agentic_harness.runner import run_trial

    bench = BashOperations()
    task = next(t for t in bench.tasks() if t.id == "bash-001")
    bench.setup([task])
    outcome = run_trial(bench, ClaudeCode(os.environ["HARNESS_MODEL"]), task, Limits(), tmp_path, DockerEnvironment)
    print(f"live cost: ${outcome.cost_usd:.4f}, exit: {outcome.agent_exit_status}")
    assert (outcome.status, outcome.score.value, outcome.agent_exit_status) == ("graded", True, "success"), outcome.error
    kinds = [json.loads(line)["kind"] for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert (tmp_path / "trajectory.jsonl").exists()
    assert os.environ["ANTHROPIC_API_KEY"] not in "".join(p.read_text() for p in tmp_path.glob("*.json*"))
    assert _no_leftovers()


@pytest.mark.live
@LIVE
def test_live_calls_a_tool_on_the_mcp_fixture_sidecar(tmp_path):
    from test_mcp import FIXTURE, SERVER, TASK, WithMcp

    from agentic_harness.environments import DockerEnvironment, build_image, ensure_image
    from agentic_harness.runner import run_trial
    from agentic_harness.types import Message, Score, Text

    build_image((FIXTURE / "Dockerfile").read_text(), context=FIXTURE, tag=SERVER.image)
    ensure_image(TASK.image)

    class SecretWord(WithMcp):
        def run_agent(self, task, agent, env):
            agent.send(Message("user", (Text("Get the secret word from the fixture server's tool and write it, "
                                             "and nothing else, to /workspace/word.txt"),)))

        def grade(self, task, grader, env, transcript):
            return Score(grader.read_file("/workspace/word.txt").strip() == b"pomegranate-42")

    outcome = run_trial(SecretWord(), ClaudeCode(os.environ["HARNESS_MODEL"]), TASK, Limits(), tmp_path, DockerEnvironment)
    print(f"live cost: ${outcome.cost_usd:.4f}, exit: {outcome.agent_exit_status}")
    assert (outcome.status, outcome.score.value) == ("graded", True), outcome.error
    calls = [json.loads(line)["payload"]["name"] for line in (tmp_path / "events.jsonl").read_text().splitlines()
             if json.loads(line)["kind"] == "tool_call"]
    assert "mcp__fixture__secret_word" in calls
    assert _no_leftovers()


def test_usage_comes_from_the_result_event_with_cached_tokens_counted_as_input():
    result = RESULT | {"usage": {"input_tokens": 10, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 3000,
                                 "output_tokens": 50}}
    assert usage([TOOL_USE, result]) == (3210, 50, 2)


def test_a_turn_cut_off_at_the_deadline_still_counts_its_tokens_and_cost(tmp_path, monkeypatch):
    def assistant(text_tokens: int) -> dict:  # Claude Code prints the same message once per content block
        return {"type": "assistant", "message": {"id": "m1", "content": [],
                                                 "usage": {"input_tokens": 1000, "output_tokens": text_tokens}}}
    world = FakeWorld([[assistant(5), assistant(400)]], timed_out=True)
    s = session(world, tmp_path, monkeypatch)
    s.send(Message("user", (Text("go"),)))
    stats = s.stats()
    assert (stats.exit_status, stats.model_calls, stats.input_tokens, stats.output_tokens) == ("TimeLimit", 1, 1000, 400)
    assert stats.cost_usd == pytest.approx(estimate_cost("claude-haiku-5-5", [assistant(400)])) and stats.cost_usd > 0
