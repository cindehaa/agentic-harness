"""harness adapt: Claude Code is called headless with the skill and a narrow tool list, then validate runs (both faked)."""

import subprocess

from click.testing import CliRunner

from agentic_harness import cli as cli_module
from agentic_harness.cli import cli


def test_adapt_drafts_with_the_skill_then_validates(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    result = CliRunner().invoke(cli, ["adapt", "benchmarks/bash-operations", "--tasks", "2", "--name", "bash-copy"])
    assert result.exit_code == 0, result.output
    (draft, prompt), (validate, _) = calls
    assert draft[:2] == ["claude", "-p"]
    assert cli_module.SKILL.read_text() in prompt and "`benchmarks/bash-operations`" in prompt and "2 simple task(s)" in prompt
    assert draft[draft.index("--permission-mode") + 1] == "acceptEdits"
    tools = draft[draft.index("--allowedTools") + 1:draft.index("--disallowedTools")]
    assert not any(t.startswith("Bash(git") for t in tools) and "Bash(.venv/bin/harness validate:*)" in tools
    assert validate[1:] == ["validate", "--benchmark", "bash-copy"]
    assert "needs human review" in result.output


def test_adapt_stops_if_drafting_fails(monkeypatch):
    monkeypatch.setattr(cli_module.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 2))
    result = CliRunner().invoke(cli, ["adapt", "benchmarks/bash-operations"])
    assert result.exit_code == 1 and "Claude Code exited 2" in result.output


def test_adapt_translates_a_litellm_style_model_for_claude_code(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_module.subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
    for model, passed in (("anthropic/claude-sonnet-5", "claude-sonnet-5"), ("sonnet", "sonnet")):
        CliRunner().invoke(cli, ["adapt", "benchmarks/bash-operations", "--model", model])
        draft = calls[-2]
        assert draft[draft.index("--model") + 1] == passed
