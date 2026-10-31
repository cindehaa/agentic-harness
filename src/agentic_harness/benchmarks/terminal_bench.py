"""Adapter for three tasks of benchmarks/terminal-bench (original-tasks/). It only moves things: each task's own
Dockerfile builds the world, its own run-tests.sh + tests/ judge, and the agent hears only task.yaml's instruction.

Fidelity note:
- Reused unchanged: every task's Dockerfile (built with the task folder as context), task.yaml (instruction and
  timeouts), run-tests.sh and tests/ (run as Terminal-Bench does: copied into the agent's own container only at grade
  time as /tests, TEST_DIR=/tests, run from the image's WORKDIR), and solution.sh / solution.yaml as reference solutions.
- Ported: the result parser (terminal_bench/parsers/pytest_parser.py, reduced to its core): the tests' statuses are
  read from pytest's "short test summary info" section; PASSED / SKIPPED / XFAIL count as passed, anything else as
  failed, and the task passes only if every test passed (as Terminal-Bench's is_resolved).
- Kept as they are: run-tests.sh installs curl and uv from the network at grade time (so grading needs internet), and
  fix-permissions grades its relative "./process_data.sh" from /app; sqlite-db-truncate is a "medium" task.
- Differences: the agent starts in /workspace, not in the image's WORKDIR (/app); the instructions name /app paths
  absolutely. The agent is mini-swe-agent / Claude Code, not Terminal-Bench's terminus (tmux) agents. Terminal-Bench's
  per-task max_agent_timeout_sec is not applied (the harness has its own trial limits); max_test_timeout_sec is.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentic_harness.agents.base import AgentSession
from agentic_harness.benchmarks.base import Benchmark, GraderError, content_hash
from agentic_harness.environments import Environment, build_image, ensure_image
from agentic_harness.paths import REPO_ROOT
from agentic_harness.types import Message, ReferenceSolution, Score, Task, Text, Transcript

ROOT = REPO_ROOT / "benchmarks" / "terminal-bench" / "original-tasks"
TASK_IDS = ("hello-world", "fix-permissions", "sqlite-db-truncate")  # few dependencies, quick to build and grade
TESTS_DIR = "/tests"  # where Terminal-Bench puts tests/ and run-tests.sh in the agent's container
WORKDIR = "/app"  # the WORKDIR of these tasks' base images; run-tests.sh refuses to run from /
PASSING = {"PASSED", "SKIPPED", "XFAIL"}  # pytest_parser.py: these map to UnitTestStatus.PASSED


@dataclass(frozen=True)
class TerminalBenchTask(Task):
    instruction: str  # the benchmark's own wording (task.yaml), sent to the agent verbatim
    max_test_timeout_s: float  # task.yaml's max_test_timeout_sec: how long run-tests.sh may take


class TerminalBench(Benchmark[TerminalBenchTask]):
    name = "terminal-bench"

    def tasks(self) -> list[TerminalBenchTask]:
        tasks = []
        for task_id in TASK_IDS:
            meta = yaml.safe_load((ROOT / task_id / "task.yaml").read_text())
            tasks.append(TerminalBenchTask(
                benchmark=self.name,
                id=task_id,
                image=f"harness-terminal-bench:{task_id}-{self.version()}",
                instruction=meta["instruction"],
                max_test_timeout_s=float(meta["max_test_timeout_sec"]),
            ))
        return tasks

    def version(self) -> str:
        return hashlib.sha256("".join(content_hash(ROOT / t) for t in TASK_IDS).encode()).hexdigest()[:12]

    def setup(self, tasks: list[TerminalBenchTask]) -> None:
        for t in tasks:  # pulled explicitly and time-bounded, not inside a build
            for line in (ROOT / t.id / "Dockerfile").read_text().splitlines():
                if line.startswith("FROM "):
                    ensure_image(line.split()[1])
        for t in tasks:
            build_image((ROOT / t.id / "Dockerfile").read_text(), context=ROOT / t.id, tag=t.image)

    def run_agent(self, task: TerminalBenchTask, agent: AgentSession, env: Environment) -> None:
        agent.send(Message("user", (Text(task.instruction),)))

    def reference_solution(self, task: TerminalBenchTask) -> ReferenceSolution | None:
        folder = ROOT / task.id
        if (folder / "solution.sh").exists():
            script = (folder / "solution.sh").read_text()
        elif (folder / "solution.yaml").exists():  # a list of shell commands, run one after the other
            script = "\n".join(step["command"] for step in yaml.safe_load((folder / "solution.yaml").read_text()))
        else:
            return None
        return ReferenceSolution("benchmark-provided", script=script, cwd=WORKDIR)  # the benchmark runs it in the WORKDIR

    def grade(self, task: TerminalBenchTask, grader: Environment, env: Environment, transcript: Transcript) -> Score:
        # Terminal-Bench's own design: tests are copied into the agent's container only after the agent is done, and
        # the task's files (e.g. /app) live in that container, not in the shared /workspace, so grade through `env`
        env.upload(ROOT / task.id / "tests", TESTS_DIR)
        env.upload(ROOT / task.id / "run-tests.sh", f"{TESTS_DIR}/run-tests.sh")
        r = env.exec(["bash", f"{TESTS_DIR}/run-tests.sh"], timeout=task.max_test_timeout_s, cwd=WORKDIR,
                     env={"TEST_DIR": TESTS_DIR})
        if r.timed_out:
            return Score(value=False, explanation=f"tests timed out after {task.max_test_timeout_s:g}s",
                         metadata={"timed_out": True, "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]})
        results = _parse_pytest(r.stdout, r.stderr)
        failed = sorted(name for name, status in results.items() if status not in PASSING)
        return Score(value=not failed and bool(results),
                     explanation=f"{len(results) - len(failed)}/{len(results)} tests passed" + (f"; failed: {failed}" if failed else ""),
                     metadata={"tests": results, "exit_code": r.exit_code, "stderr": r.stderr[-2000:]})


def _parse_pytest(stdout: str, stderr: str) -> dict[str, str]:
    """test name -> status, from pytest's `-rA` short test summary (terminal_bench's PytestParser). If run-tests.sh
    never got to pytest (e.g. the uv download failed) there is no summary: a grader failure, not a failed task."""
    parts = re.split(r"=+\s*short test summary info\s*=+", stdout, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) < 2:
        raise GraderError(f"no pytest summary in the grader output: stdout={stdout[-2000:]!r} stderr={stderr[-2000:]!r}")
    results = {}
    for line in parts[1].splitlines():
        if line.startswith("FAILED"):
            line = " - ".join(line.split(" - ")[:-1]) if " - " in line else line
        pieces = line.split(maxsplit=1)
        if len(pieces) == 2 and pieces[0].strip(":") in {"PASSED", "FAILED", "SKIPPED", "XFAIL", "XPASS", "ERROR"}:
            name = pieces[1].strip().split("::", maxsplit=1)[-1]
            if name:
                results[name] = pieces[0].strip(":")
    return results
