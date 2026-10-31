import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentic_harness.agents.base import AgentSession
from agentic_harness.benchmarks.base import Benchmark, BenchmarkUnavailable, GraderError, content_hash
from agentic_harness.environments import Environment, build_image, ensure_image
from agentic_harness.paths import REPO_ROOT
from agentic_harness.types import LimitOverrides, Message, ReferenceSolution, Score, Task, Text, Transcript

ROOT = REPO_ROOT / "benchmarks" / "terminal-bench" / "original-tasks"
TASK_IDS = ("hello-world", "log-summary", "sqlite-db-truncate")
TESTS_DIR = "/tests"
WORKDIR = "/app"  # a test checks it; run-tests.sh refuses to run from /
PASSING = {"PASSED", "SKIPPED", "XFAIL"}
DEFAULT_AGENT_TIMEOUT_S, DEFAULT_TEST_TIMEOUT_S = 360.0, 60.0


@dataclass(frozen=True)
class TerminalBenchTask(Task):
    instruction: str
    max_agent_timeout_s: float
    max_test_timeout_s: float


class TerminalBench(Benchmark[TerminalBenchTask]):
    name = "terminal-bench"

    def tasks(self) -> list[TerminalBenchTask]:
        if not ROOT.is_dir():
            raise BenchmarkUnavailable(f"{ROOT} is missing: run `make init-extra-benchmarks BENCH=benchmarks/terminal-bench`")
        tasks = []
        for task_id in TASK_IDS:
            meta = yaml.safe_load((ROOT / task_id / "task.yaml").read_text())
            tasks.append(TerminalBenchTask(
                benchmark=self.name,
                id=task_id,
                image=f"harness-terminal-bench:{task_id}-{self.version()}",
                instruction=meta["instruction"],
                max_agent_timeout_s=float(meta.get("max_agent_timeout_sec", DEFAULT_AGENT_TIMEOUT_S)),
                max_test_timeout_s=float(meta.get("max_test_timeout_sec", DEFAULT_TEST_TIMEOUT_S)),
                workdir=WORKDIR,
            ))
        return tasks

    def version(self) -> str:
        return hashlib.sha256("".join(content_hash(ROOT / t) for t in TASK_IDS).encode()).hexdigest()[:12]

    def setup(self, tasks: list[TerminalBenchTask]) -> None:
        for t in tasks:
            for line in (ROOT / t.id / "Dockerfile").read_text().splitlines():
                if line.startswith("FROM "):
                    ensure_image(line.split()[1])
        for t in tasks:
            build_image((ROOT / t.id / "Dockerfile").read_text(), context=ROOT / t.id, tag=t.image)

    def limits(self, task: TerminalBenchTask) -> LimitOverrides:
        return LimitOverrides(trial_timeout_s=task.max_agent_timeout_s, grade_timeout_s=task.max_test_timeout_s)

    def run_agent(self, task: TerminalBenchTask, agent: AgentSession, env: Environment) -> None:
        agent.send(Message("user", (Text(task.instruction),)))

    def reference_solution(self, task: TerminalBenchTask) -> ReferenceSolution | None:
        solution = ROOT / task.id / "solution.sh"
        if not solution.exists():
            return None
        # as Terminal-Bench's oracle: bash (not sh), from the WORKDIR
        return ReferenceSolution("benchmark-provided", files={"/oracle/solution.sh": solution.read_text()},
                                 script="bash /oracle/solution.sh", cwd=WORKDIR)

    def grade(self, task: TerminalBenchTask, grader: Environment, env: Environment, transcript: Transcript) -> Score:
        # as Terminal-Bench: tests enter the agent's world only after it is done
        env.upload(ROOT / task.id / "tests", TESTS_DIR)
        env.upload(ROOT / task.id / "run-tests.sh", f"{TESTS_DIR}/run-tests.sh")
        # no timeout: the rest of the grade timeout
        r = env.exec(["bash", f"{TESTS_DIR}/run-tests.sh"], timeout=None, cwd=WORKDIR, env={"TEST_DIR": TESTS_DIR})
        if r.timed_out:  # as Terminal-Bench: a test timeout fails the task
            return Score(value=False, explanation="tests timed out (grade timeout)",
                         metadata={"timed_out": True, "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]})
        results = _parse_pytest(r.stdout, r.stderr)
        failed = sorted(name for name, status in results.items() if status not in PASSING)
        return Score(value=not failed and bool(results),
                     explanation=f"{len(results) - len(failed)}/{len(results)} tests passed" + (f"; failed: {failed}" if failed else ""),
                     metadata={"tests": results, "exit_code": r.exit_code, "stderr": r.stderr[-2000:]})


def _parse_pytest(stdout: str, stderr: str) -> dict[str, str]:
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
