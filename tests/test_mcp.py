"""MCP servers a benchmark declares: run as sidecars on the trial's network (real Docker, no model). The server is a
test fixture (tests/fixtures/mcp-server), not a registered benchmark."""

import json
import subprocess
from pathlib import Path

import pytest

from agentic_harness.agents import Agent, AgentCapabilities, AgentSession
from agentic_harness.agents.mini_swe_agent import MiniSweAgent
from agentic_harness.benchmarks import Benchmark
from agentic_harness.environments import DockerEnvironment, EnvironmentLost, Sidecar, build_image
from agentic_harness.runner import run_trial
from agentic_harness.types import Limits, McpServer, Message, Score, Task, Text
from agentic_harness.environments import docker as docker_module

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    pytest.skip("Docker is not running", allow_module_level=True)

FIXTURE = Path(__file__).parent / "fixtures" / "mcp-server"
SERVER_IMAGE = "harness-test-mcp-server"
SERVER = McpServer("fixture", SERVER_IMAGE, ("python", "/server.py"), 8000)
WORLD = "python:3.11-slim"  # has python, so the test can speak MCP from inside the world without curl
TASK = Task("mcp-fixture", "t1", WORLD)

# An MCP initialize request over streamable HTTP, sent from inside the world; prints the server's name
INITIALIZE = """
import json, sys, urllib.request
body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
req = urllib.request.Request(sys.argv[1], json.dumps(body).encode(),
                             {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
reply = urllib.request.urlopen(req, timeout=10).read().decode()
print(json.loads(reply.split("data: ", 1)[1])["result"]["serverInfo"]["name"])
"""


@pytest.fixture(scope="module", autouse=True)
def images():
    build_image((FIXTURE / "Dockerfile").read_text(), context=FIXTURE, tag=SERVER_IMAGE)
    subprocess.run(["docker", "pull", "-q", WORLD], capture_output=True)


def _labelled(*cmd: str) -> str:
    return subprocess.run(["docker", *cmd, "-q", "--filter", f"label={docker_module.LABEL}"], capture_output=True, text=True).stdout


def test_service_sidecar_is_reachable_from_the_world_shares_only_the_network_and_is_removed():
    with DockerEnvironment(WORLD) as env:
        service = env.attach(Sidecar(SERVER.image, SERVER.command, SERVER.port, shares_world=False))
        service.start()  # returns once the server listens
        r = env.exec(["python", "-c", INITIALIZE, SERVER.url])
        assert (r.exit_code, r.stdout) == (0, "fixture\n"), r.stderr
        assert "server.py" not in env.exec(["sh", "-c", "cat /proc/[0-9]*/cmdline"]).stdout  # its processes are hidden
        env.write_file("only-in-world.txt", "x")
        assert service.exec(["test", "-e", "/workspace/only-in-world.txt"]).exit_code != 0  # no shared workspace
        assert "Uvicorn running" in subprocess.run(["docker", "logs", service.container_id], capture_output=True, text=True).stderr
    assert service.container_id is None
    assert _labelled("ps", "-a") == "" and _labelled("volume", "ls") == ""


def test_service_that_never_listens_is_environment_lost_with_its_logs(monkeypatch):
    monkeypatch.setattr("agentic_harness.environments.docker.SERVICE_START_TIMEOUT", 2)
    with DockerEnvironment(WORLD) as env:
        with pytest.raises(EnvironmentLost, match="not listening on port 8000.*boom"):
            env.attach(Sidecar(WORLD, ("sh", "-c", "echo boom >&2; sleep 60"), 8000, shares_world=False)).start()


class WithMcp(Benchmark[Task]):
    """One task whose actions are served by the fixture MCP server; passes if the agent wrote the server's name."""

    name = "mcp-fixture"

    def tasks(self): return [TASK]
    def mcp_servers(self, task): return [SERVER]
    def run_agent(self, task, agent, env): agent.send(Message("user", (Text("ask the server its name"),)))

    def grade(self, task, grader, env, transcript):
        return Score(grader.read_file("/workspace/name.txt") == b"fixture\n")


class McpScriptedAgent(Agent):
    """Speaks to every MCP server it is given from inside the world, writing the reply to name.txt."""

    name = "mcp-scripted"
    capabilities = AgentCapabilities(mcp=True)

    def start(self, env, artifacts_dir, limits, mcp_servers=()) -> AgentSession:
        class Session(AgentSession):
            def send(self, message: Message) -> Message:
                for server in mcp_servers:
                    env.write_file("/tmp/init.py", INITIALIZE)
                    env.exec(f"python /tmp/init.py {server.url} > /workspace/name.txt")
                return Message("agent", (Text("done"),))

        return Session()


def test_runner_starts_the_declared_servers_before_the_agent_and_removes_them(tmp_path):
    outcome = run_trial(WithMcp(), McpScriptedAgent("none"), TASK, Limits(), tmp_path, DockerEnvironment)
    assert (outcome.status, outcome.score.value) == ("graded", True), outcome.error
    assert _labelled("ps", "-a") == "" and _labelled("volume", "ls") == ""


def test_benchmark_declaring_mcp_servers_makes_minis_trial_unsupported(tmp_path):
    outcome = run_trial(WithMcp(), MiniSweAgent("anthropic/claude-haiku-5-5"), TASK, Limits(), tmp_path, DockerEnvironment)
    assert (outcome.status, outcome.error) == ("unsupported", "mini-swe-agent can't do what this task needs: mcp")
    assert json.loads((tmp_path / "outcome.json").read_text())["status"] == "unsupported"
