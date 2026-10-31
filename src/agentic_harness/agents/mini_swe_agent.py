"""mini-swe-agent, run on the host through its Python API. Its commands run in the trial's container via our
Environment, so the API key never enters the container. Prompts and templates are mini's own (`mini.yaml`, what
the `mini` CLI uses); the harness adds nothing to them."""

import os
import subprocess
import time
from pathlib import Path

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")  # mini prints a banner on import otherwise

import litellm  # noqa: E402
from litellm.exceptions import (  # noqa: E402
    APIConnectionError,
    BadGatewayError,
    ContentPolicyViolationError,
    ContextWindowExceededError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
)
from minisweagent.agents.default import DefaultAgent  # noqa: E402
from minisweagent.config import get_config_from_spec  # noqa: E402
from minisweagent.exceptions import InterruptAgentFlow, LimitsExceeded, Submitted  # noqa: E402
from minisweagent.models import get_model  # noqa: E402

from agentic_harness.agents.base import (  # noqa: E402
    Agent, AgentCapabilities, AgentLimitation, AgentSession, AgentStats, PreflightError, TransientError, Unsupported,
)
from agentic_harness.environments.base import Environment  # noqa: E402
from agentic_harness.paths import REPO_ROOT  # noqa: E402
from agentic_harness.types import Limits, McpServer, Message, Text  # noqa: E402

MODEL_TIMEOUT_S = 120  # one model call (litellm `timeout`); mini retries failed calls itself
# litellm errors that mean the model itself can't go on (not our setup or the provider): grade the world as it is.
# mini re-raises them as they are (after logging an "exit" message whose exit_status is the class name).
LIMITATIONS = (ContextWindowExceededError, ContentPolicyViolationError)
# litellm errors that are temporary and not the agent's doing (network down incl. timeouts, rate limited, overloaded
# = InternalServerError, 5xx), still failing after mini's own retries: infra_error, retried
TRANSIENT = (APIConnectionError, RateLimitError, InternalServerError, ServiceUnavailableError, BadGatewayError)


class TimeLimit(LimitsExceeded):
    """The trial deadline passed. A LimitsExceeded, so mini's loop ends cleanly with exit status "TimeLimit"."""


class ContainerEnv:
    """mini's Environment protocol (execute / get_template_vars / serialize) on top of our Environment."""

    def __init__(self, env: Environment, deadline: float, command_timeout_s: float) -> None:
        self.env = env
        self.deadline = deadline  # a time.monotonic() value
        self.config = {"image": env.image, "command_timeout_s": command_timeout_s}  # the protocol has a config
        self.outputs: list[dict] = []  # outputs of the current step's commands (MiniAgent.resume needs them)
        # The container's OS, not the host's: mini's own environments report platform.uname()
        r = env.exec("uname -s; uname -r; uname -v; uname -m")
        self.uname = dict(zip(("system", "release", "version", "machine"), r.stdout.splitlines()))

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict:
        if time.monotonic() > self.deadline:
            raise TimeLimit({"role": "exit", "content": "TimeLimit", "extra": {"exit_status": "TimeLimit", "submission": ""}})
        timeout = timeout or self.config["command_timeout_s"]
        # mini wants one output stream, in order (its envs use stderr=STDOUT): `exec 2>&1` does that in the shell
        r = self.env.exec("exec 2>&1\n" + action.get("command", ""), timeout=timeout, cwd=cwd or None)
        output = {"output": r.stdout + r.stderr, "returncode": r.exit_code, "exception_info": ""}
        if r.timed_out:
            output["exception_info"] = f"The command timed out after {timeout} seconds."
        self.outputs.append(output)
        _check_finished(output)
        return output

    def get_template_vars(self, **kwargs) -> dict:
        return self.uname | kwargs

    def serialize(self) -> dict:
        return {"info": {"config": {"environment": self.config, "environment_type": f"{__name__}.ContainerEnv"}}}


def _check_finished(output: dict) -> None:
    """Raise Submitted when the agent submits. Same as mini's environments/local.py::_check_finished."""
    lines = output.get("output", "").lstrip().splitlines(keepends=True)
    if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and output["returncode"] == 0:
        submission = "".join(lines[1:])
        raise Submitted({"role": "exit", "content": submission, "extra": {"exit_status": "Submitted", "submission": submission}})


class MiniAgent(DefaultAgent):
    """mini's DefaultAgent plus resume(): continue the same history with a new user message."""

    def execute_actions(self, message: dict) -> list[dict]:
        self.env.outputs = []
        return super().execute_actions(message)

    def resume(self, text: str) -> dict:
        self.messages.pop()  # mini's "exit" marker; the model API doesn't know that role
        last = self.messages[-1]
        if last["role"] == "assistant":  # it ended inside a command (submit, time limit): the API needs its result
            self.add_messages(*self.model.format_observation_messages(last, self.env.outputs, self.get_template_vars()))
        self.add_messages(self.model.format_message(role="user", content=text))
        while True:  # the same loop as DefaultAgent.run()
            try:
                self.step()
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
            except Exception as e:
                self.handle_uncaught_exception(e)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                break
        return self.messages[-1].get("extra", {})


class MiniSweSession(AgentSession):
    def __init__(self, agent: MiniAgent) -> None:
        self.agent = agent

    def send(self, message: Message) -> Message:
        if not all(isinstance(c, Text) for c in message.content):
            raise Unsupported("mini-swe-agent only reads Text")
        text = "\n".join(c.text for c in message.content)
        try:
            # First turn: mini's own run() (its system + instance templates). Later turns continue the history.
            info = self.agent.resume(text) if self.agent.messages else self.agent.run(text)
        except LIMITATIONS as e:
            raise AgentLimitation(f"{type(e).__name__}: {e}") from e
        except TRANSIENT as e:
            raise TransientError(f"{type(e).__name__}: {e}") from e
        return Message("agent", (Text(info.get("submission", "")),))

    def stats(self) -> AgentStats:
        # mini logs an "exit" message when it stops, even when it crashed: its exit_status is the reason
        exit_status = self.agent.messages[-1].get("extra", {}).get("exit_status") if self.agent.messages else None
        usages = [(m.get("extra", {}).get("response") or {}).get("usage") or {} for m in self.agent.messages]
        return AgentStats(exit_status, self.agent.cost, self.agent.n_calls,
                          sum(u.get("prompt_tokens") or 0 for u in usages), sum(u.get("completion_tokens") or 0 for u in usages))


class MiniSweAgent(Agent):
    name = "mini-swe-agent"
    # bash only: mini acts solely through shell commands, so a task needing MCP tools is unsupported
    capabilities = AgentCapabilities(content_types=frozenset({"text"}), multi_turn=True, mcp=False)

    def preflight(self) -> None:
        """The model is one litellm knows, and its provider's API key is set. No auth call: that would spend tokens."""
        try:
            litellm.get_llm_provider(self.model)
        except Exception as e:
            raise PreflightError(f"model {self.model!r} is not one litellm knows: {e}") from None
        missing = litellm.validate_environment(self.model)["missing_keys"]
        if missing:
            raise PreflightError(f"model {self.model!r} needs {', '.join(missing)} (set it in .env)")

    def version(self) -> str | None:
        """The agents/mini-swe-agent submodule's commit."""
        result = subprocess.run(["git", "-C", str(REPO_ROOT / "agents" / "mini-swe-agent"), "rev-parse", "HEAD"],
                                capture_output=True, text=True)
        return result.stdout.strip() or None

    def start(self, env: Environment, artifacts_dir: Path, limits: Limits, mcp_servers: tuple[McpServer, ...] = ()) -> MiniSweSession:
        config = get_config_from_spec("mini.yaml")
        model_config = config["model"] | {"model_kwargs": config["model"].get("model_kwargs", {}) | {"timeout": MODEL_TIMEOUT_S}}
        agent = MiniAgent(
            get_model(self.native_model(), model_config),
            ContainerEnv(env, time.monotonic() + limits.trial_timeout_s, limits.command_timeout_s),
            **config["agent"] | {
                "step_limit": limits.max_steps,
                "cost_limit": limits.cost_usd,
                "output_path": artifacts_dir / "trajectory.json",  # mini saves it after every step
            },
        )
        return MiniSweSession(agent)
