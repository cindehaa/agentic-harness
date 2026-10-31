import importlib
import os
from collections.abc import Callable
from functools import partial
from pathlib import Path

from agentic_harness.agents.base import (
    MODEL_ENV, Agent, AgentCapabilities, AgentLimitation, AgentSession, AgentStats, PreflightError, TransientError, Unsupported,
)

# imported only when used, so `harness status` never loads litellm
AGENTS: dict[str, str] = {
    "mini-swe-agent": "agentic_harness.agents.mini_swe_agent:MiniSweAgent",
}


def resolve_agent(name_or_path: str) -> Callable[[str], Agent]:
    from agentic_harness.agents.acp import AcpAgent, built_in_definitions, load_definition

    if name_or_path.endswith(".toml"):
        path = Path(name_or_path).resolve()  # runs.agent keeps the full path: a worker started elsewhere finds the file
        if not path.is_file():
            raise ValueError(f"no agent definition file {name_or_path!r}")
        return partial(AcpAgent, load_definition(path))
    name = Path(name_or_path).name
    if name in AGENTS:
        module, _, cls = AGENTS[name].partition(":")
        return getattr(importlib.import_module(module), cls)
    acp_agents = built_in_definitions()
    if name in acp_agents:
        return partial(AcpAgent, acp_agents[name])
    raise ValueError(f"unknown agent {name!r}; known: {', '.join([*AGENTS, *acp_agents])}, or an ACP agent's .toml file")


def resolve_model(model: str | None) -> str | None:
    return model or os.environ.get(MODEL_ENV) or None


__all__ = [
    "AGENTS", "Agent", "AgentCapabilities", "AgentLimitation", "AgentSession", "AgentStats", "PreflightError", "TransientError", "Unsupported",
    "MODEL_ENV", "resolve_agent", "resolve_model",
]
