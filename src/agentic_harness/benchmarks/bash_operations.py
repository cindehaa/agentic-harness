"""Adapter for benchmarks/bash-operations. It only moves things: the benchmark's own setup.py builds the world,
its own evaluate.py judges, and the agent hears only the dataset's problem_statement."""

import json
from dataclasses import dataclass
from pathlib import Path

from agentic_harness.agents.base import AgentSession
from agentic_harness.benchmarks.base import Benchmark, GraderError, content_hash
from agentic_harness.environments import Environment, build_image, ensure_image
from agentic_harness.original import OriginalAgentWorld, run_script
from agentic_harness.paths import REPO_ROOT
from agentic_harness.types import Message, OriginalResult, ReferenceSolution, Score, Task, Text, Transcript

ROOT = REPO_ROOT / "benchmarks" / "bash-operations"
GRADER_IMAGE = "python:3.11-slim"  # evaluate.py needs python; the agent's world (alpine) has none, and never gets it

# The benchmark ships no solutions: ours, written from each problem statement and its verification spec
SOLUTIONS = {
    "bash-001": "echo 'Hello World' > hello.txt",
    "bash-002": "mkdir test_dir && touch test_dir/file1.txt test_dir/file2.txt test_dir/file3.txt",
    "bash-003": "grep -c '' input.txt > count.txt",  # not `wc -l`: input.txt has no trailing newline, so wc says 4
    "bash-004": "ls *.txt > txt_files.txt",
    "bash-005": "seq 1 10 > numbers.txt",
}


@dataclass(frozen=True)
class BashTask(Task):
    problem_statement: str  # the benchmark's own wording, sent to the agent verbatim
    base_image: str  # the image the benchmark specifies, e.g. "alpine:latest"


# Stage 1 runs the benchmark's own setup.py (needs python); stage 2 is the benchmark's base image
# with only setup's output copied in, so the agent's world never gains python.
DOCKERFILE = """\
FROM python:3.11-slim AS setup
COPY . /bench
RUN python /bench/setup.py --workspace-dir /out --task {task_id}

FROM {base_image}
COPY --from=setup /out /workspace
"""


class BashOperations(Benchmark[BashTask]):
    name = "bash-operations"

    def tasks(self) -> list[BashTask]:
        rows = json.loads((ROOT / "dataset.json").read_text())
        return [
            BashTask(
                benchmark=self.name,
                id=r["task_id"],
                image=f"harness-bash-operations:{r['task_id']}-{self.version()}",
                problem_statement=r["problem_statement"],
                base_image=r["docker_image"],
            )
            for r in rows
        ]

    def version(self) -> str:
        return content_hash(ROOT, DOCKERFILE)

    def setup(self, tasks: list[BashTask]) -> None:
        # Pulled now, explicitly and time-bounded: the grader's image (so grading needs no network) and every image
        # the builds start from (the setup stage is python too)
        for image in sorted({GRADER_IMAGE, *(t.base_image for t in tasks)}):
            ensure_image(image)
        for t in tasks:
            build_image(DOCKERFILE.format(task_id=t.id, base_image=t.base_image), context=ROOT, tag=t.image)

    def run_agent(self, task: BashTask, agent: AgentSession, env: Environment) -> None:
        agent.send(Message("user", (Text(task.problem_statement),)))

    def reference_solution(self, task: BashTask) -> ReferenceSolution | None:
        script = SOLUTIONS.get(task.id)
        return ReferenceSolution("harness-written", script=script) if script else None

    def grader_image(self, task: BashTask) -> str:
        return GRADER_IMAGE

    def grade(self, task: BashTask, grader: Environment, env: Environment, transcript: Transcript) -> Score:
        grader.upload(ROOT, "/grader")  # the benchmark's own evaluate.py + dataset.json, only in the grader
        r = grader.exec(["python", "/grader/evaluate.py", "--workspace-dir", "/workspace", "--task", task.id])
        return _score(r.stdout, r.stderr)

    def original_workflow(self, task: BashTask, agent_name: str, model: str, out_dir: Path) -> OriginalResult:
        """examples/run_bash_example.sh in Docker: setup.py, the agent on the dataset's image (alpine) with the
        workspace bind-mounted, evaluate.py. The scripts run in python:3.11-slim, not on the host's python."""
        workspace = out_dir / "workspace"
        workspace.mkdir(parents=True)
        scripts = {ROOT: "/bench:ro", workspace: "/workspace"}
        setup = run_script(GRADER_IMAGE, scripts, ["python", "/bench/setup.py", "--workspace-dir", "/workspace", "--task", task.id])
        if setup.returncode != 0:
            raise RuntimeError(f"setup.py failed: {setup.stderr}")
        with OriginalAgentWorld(agent_name, model, task.base_image, {workspace: "/workspace"}, "/workspace", out_dir) as world:
            world.run_agent(task.problem_statement)
        r = run_script(GRADER_IMAGE, scripts, ["python", "/bench/evaluate.py", "--workspace-dir", "/workspace", "--task", task.id])
        return OriginalResult(_score(r.stdout, r.stderr), world.cost, world.exit_status)


def _score(stdout: str, stderr: str) -> Score:
    """evaluate.py's verdict. It exits 1 on a failed task, so judge by its JSON, not its exit code."""
    try:
        result = json.loads(stdout)
        passed = result["passed"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise GraderError(f"unreadable grader output ({e}): stdout={stdout!r} stderr={stderr!r}") from None
    return Score(value=passed, explanation=str(result.get("details")), metadata={"grader": result, "stderr": stderr})
