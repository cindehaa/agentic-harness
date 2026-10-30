"""Two built-in fake agents for `harness validate`: they check an adapter's wiring without a model. Not in AGENTS:
nobody runs them as a real agent."""

from pathlib import Path

from agentic_harness.agents.base import Agent, AgentCapabilities, AgentSession
from agentic_harness.environments.base import Environment
from agentic_harness.types import Limits, McpServer, Message, ReferenceSolution, Text

# They never read a message, so no task's needs make them unsupported: validate must reach every task's grader
ANYTHING = AgentCapabilities(content_types=frozenset({"text", "file", "data"}), multi_turn=True, mcp=True)


class NullSession(AgentSession):
    def send(self, message: Message) -> Message:
        return Message("agent", (Text(""),))


class NullAgent(Agent):
    """Does nothing. Every task must then fail: a pass means a grader that passes anything, or a world that already
    holds the answer."""

    name = "null"
    capabilities = ANYTHING

    def __init__(self) -> None:
        super().__init__(model="none")

    def start(self, env: Environment, artifacts_dir: Path, limits: Limits, mcp_servers: tuple[McpServer, ...] = ()) -> NullSession:
        return NullSession()


class OracleSession(AgentSession):
    def __init__(self, env: Environment, solution: ReferenceSolution, timeout_s: float) -> None:
        self.env = env
        self.solution = solution
        self.timeout_s = timeout_s
        self.exit_code: int | None = None  # the script's, once applied; validate reports it when the task fails
        self.output = ""

    def send(self, message: Message) -> Message:
        if self.exit_code is not None:  # a later turn: the answer is already in the world
            return Message("agent", (Text(""),))
        for path, text in self.solution.files.items():
            self.env.write_file(path, text)
        self.exit_code = 0
        if self.solution.script:
            r = self.env.exec(self.solution.script, timeout=self.timeout_s, cwd=self.solution.cwd)
            self.exit_code, self.output = r.exit_code, (r.stdout + r.stderr)[-2000:]
        return Message("agent", (Text(f"reference solution applied (exit {self.exit_code})"),))


class OracleAgent(Agent):
    """Applies one task's reference solution, whatever it is told. Every task must then pass: a fail means a broken
    world, a wrong path, or a misread grader output. One instance per task."""

    name = "oracle"
    capabilities = ANYTHING

    def __init__(self, solution: ReferenceSolution) -> None:
        super().__init__(model="none")
        self.solution = solution
        self.session: OracleSession | None = None  # kept, so validate can report what the script did

    def start(self, env: Environment, artifacts_dir: Path, limits: Limits, mcp_servers: tuple[McpServer, ...] = ()) -> OracleSession:
        self.session = OracleSession(env, self.solution, limits.trial_timeout_s)
        return self.session
