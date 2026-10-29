"""Adapter for benchmarks/python-tasks. It only moves things: the benchmark's own prepare.py builds the world,
its own grade.py judges, and the agent hears only the dataset's prompt."""

import json
from dataclasses import dataclass

from agentic_harness.agents.base import AgentSession
from agentic_harness.benchmarks.base import Benchmark, GraderError
from agentic_harness.environments import Environment, build_image
from agentic_harness.paths import REPO_ROOT
from agentic_harness.types import Message, Score, Task, Text, Transcript

ROOT = REPO_ROOT / "benchmarks" / "python-tasks"
GRADE_TIMEOUT = 120  # seconds; grade.py stops pytest at 30s itself, the rest is python startup + slack


@dataclass(frozen=True)
class PythonTask(Task):
    prompt: str  # the benchmark's own wording, sent to the agent verbatim
    base_image: str  # the image the benchmark specifies, e.g. "python:3.11-slim"


# One stage: prepare.py pip-installs pytest and writes test_solution.py + a solution.py stub, which is the
# world the benchmark intends (pytest stays available to the agent). The benchmark's files are removed after.
DOCKERFILE = """\
FROM {base_image}
COPY . /bench
RUN python /bench/prepare.py {task_id} --workspace /workspace && rm -rf /bench
"""


class PythonTasks(Benchmark[PythonTask]):
    name = "python-tasks"

    def tasks(self) -> list[PythonTask]:
        rows = json.loads((ROOT / "dataset.json").read_text())
        return [
            PythonTask(
                benchmark=self.name,
                id=r["id"],
                image=f"harness-python-tasks:{r['id']}",
                prompt=r["prompt"],
                base_image=r["docker_image"],
            )
            for r in rows
        ]

    def setup(self, tasks: list[PythonTask]) -> None:
        for t in tasks:
            build_image(DOCKERFILE.format(task_id=t.id, base_image=t.base_image), context=ROOT, tag=t.image)

    def run_agent(self, task: PythonTask, agent: AgentSession, env: Environment) -> None:
        agent.send(Message("user", (Text(task.prompt),)))

    def protected_files(self, task: PythonTask) -> list[str]:
        return ["/workspace/test_solution.py"]  # the tests grade.py runs; the agent can edit them (benchmark as designed)

    def grade(self, task: PythonTask, env: Environment, transcript: Transcript) -> Score:
        env.upload(ROOT, "/grader")  # the benchmark's own grade.py + dataset.json, copied in late
        r = env.exec(["python", "/grader/grade.py", task.id, "--workspace", "/workspace"], timeout=GRADE_TIMEOUT)
        # grade.py always exits 0, so judge only by its JSON
        try:
            result = json.loads(r.stdout)
            passed = result["passed"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise GraderError(f"unreadable grader output ({e}): stdout={r.stdout!r} stderr={r.stderr!r}") from None
        return Score(value=passed, explanation=str(result.get("details")), metadata={"grader": result, "stderr": r.stderr})
