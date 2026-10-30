"""Adapter for benchmarks/bash-operations. It only moves things: the benchmark's own setup.py builds the world,
its own evaluate.py judges, and the agent hears only the dataset's problem_statement."""

import json
from dataclasses import dataclass

from agentic_harness.agents.base import AgentSession
from agentic_harness.benchmarks.base import Benchmark, GraderError, GraderSetupError, content_hash
from agentic_harness.environments import Environment, build_image
from agentic_harness.paths import REPO_ROOT
from agentic_harness.types import Message, Score, Task, Text, Transcript

ROOT = REPO_ROOT / "benchmarks" / "bash-operations"


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
        for t in tasks:
            build_image(DOCKERFILE.format(task_id=t.id, base_image=t.base_image), context=ROOT, tag=t.image)

    def run_agent(self, task: BashTask, agent: AgentSession, env: Environment) -> None:
        agent.send(Message("user", (Text(task.problem_statement),)))

    def grade(self, task: BashTask, env: Environment, transcript: Transcript) -> Score:
        # Grader dependency, installed only after the agent is done. It needs the network: a failure is infra's
        r = env.exec("apk add --no-cache python3", timeout=300)
        if r.exit_code != 0:
            raise GraderSetupError(f"installing python3 failed: {r.stderr}")
        env.upload(ROOT, "/grader")  # the benchmark's own evaluate.py + dataset.json, copied in late
        r = env.exec(["python3", "/grader/evaluate.py", "--workspace-dir", "/workspace", "--task", task.id])
        # evaluate.py exits 1 on a failed task, so judge by its JSON, not its exit code
        try:
            result = json.loads(r.stdout)
            passed = result["passed"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise GraderError(f"unreadable grader output ({e}): stdout={r.stdout!r} stderr={r.stderr!r}") from None
        return Score(value=passed, explanation=str(result.get("details")), metadata={"grader": result, "stderr": r.stderr})
