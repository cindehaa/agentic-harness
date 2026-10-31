"""Agents: what a benchmark talks to."""

import importlib
import os
from pathlib import Path

from agentic_harness.agents.base import (
    MODEL_ENV, Agent, AgentCapabilities, AgentLimitation, AgentSession, AgentStats, PreflightError, TransientError, Unsupported,
)

# name -> "module:Class", imported only when used, so `harness status` never loads litellm
AGENTS: dict[str, str] = {
    "mini-swe-agent": "agentic_harness.agents.mini_swe_agent:MiniSweAgent",
    "claude-code": "agentic_harness.agents.claude_code:ClaudeCode",
}


def resolve_agent(name_or_path: str) -> type[Agent]:
    """`mini-swe-agent` or a path like `agents/mini-swe-agent`: the last path part is the registry name."""
    name = Path(name_or_path).name
    if name not in AGENTS:
        raise ValueError(f"unknown agent {name!r}; known: {', '.join(AGENTS)}")
    module, _, cls = AGENTS[name].partition(":")
    return getattr(importlib.import_module(module), cls)


def resolve_model(model: str | None) -> str | None:
    """The run's model: `model` (--model), else HARNESS_MODEL; None if neither is set. Agents never read their own
    settings for it (mini's MSWEA_MODEL_NAME is only for the `mini` CLI)."""
    return model or os.environ.get(MODEL_ENV) or None


__all__ = [
    "AGENTS", "Agent", "AgentCapabilities", "AgentLimitation", "AgentSession", "AgentStats", "PreflightError", "TransientError", "Unsupported",
    "MODEL_ENV", "resolve_agent", "resolve_model",
]
