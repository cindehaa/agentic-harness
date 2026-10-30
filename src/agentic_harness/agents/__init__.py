"""Agents: what a benchmark talks to."""

import importlib
from pathlib import Path

from agentic_harness.agents.base import (
    Agent, AgentCapabilities, AgentLimitation, AgentSession, AgentStats, PreflightError, TransientError, Unsupported,
)

# name -> "module:Class", imported only when used, so `harness status` never loads litellm
AGENTS: dict[str, str] = {
    "mini-swe-agent": "agentic_harness.agents.mini_swe_agent:MiniSweAgent",
}


def resolve_agent(name_or_path: str) -> type[Agent]:
    """`mini-swe-agent` or a path like `agents/mini-swe-agent`: the last path part is the registry name."""
    name = Path(name_or_path).name
    if name not in AGENTS:
        raise ValueError(f"unknown agent {name!r}; known: {', '.join(AGENTS)}")
    module, _, cls = AGENTS[name].partition(":")
    return getattr(importlib.import_module(module), cls)


__all__ = [
    "AGENTS", "Agent", "AgentCapabilities", "AgentLimitation", "AgentSession", "AgentStats", "PreflightError", "TransientError", "Unsupported",
    "resolve_agent",
]
