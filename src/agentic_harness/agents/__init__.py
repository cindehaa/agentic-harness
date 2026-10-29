"""Agents: what a benchmark talks to."""

from pathlib import Path

from agentic_harness.agents.base import Agent, AgentSession, Unsupported
from agentic_harness.agents.mini_swe_agent import MiniSweAgent

# registry: name -> adapter class
AGENTS: dict[str, type[Agent]] = {
    "mini-swe-agent": MiniSweAgent,
}


def resolve_agent(name_or_path: str) -> type[Agent]:
    """`mini-swe-agent` or a path like `agents/mini-swe-agent`: the last path part is the registry name."""
    name = Path(name_or_path).name
    if name not in AGENTS:
        raise ValueError(f"unknown agent {name!r}; known: {', '.join(AGENTS)}")
    return AGENTS[name]


__all__ = ["AGENTS", "Agent", "AgentSession", "MiniSweAgent", "Unsupported", "resolve_agent"]
