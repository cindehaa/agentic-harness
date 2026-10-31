import json
from dataclasses import dataclass
from pathlib import Path

from agentic_harness.agents.base import AgentSession
from agentic_harness.benchmarks.base import Benchmark, GraderError, content_hash
from agentic_harness.environments import Environment, build_image, ensure_image
from agentic_harness.original import OriginalAgentWorld
from agentic_harness.paths import REPO_ROOT
from agentic_harness.types import Message, OriginalResult, ReferenceSolution, Score, Task, Text, Transcript

ROOT = REPO_ROOT / "benchmarks" / "python-tasks"

SOLUTIONS = {
    "py-001": "def add():\n    return 2 + 2\n",
    "py-002": "def fibonacci(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
    "py-003": "def get_nth(lst, n):\n    return lst[n] if 0 <= n < len(lst) else None\n",  # tests want None for -1, not lst[-1]
}


@dataclass(frozen=True)
class PythonTask(Task):
    prompt: str
    base_image: str


# one stage, so prepare.py's pytest stays available to the agent
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
                image=f"harness-python-tasks:{r['id']}-{self.version()}",
                prompt=r["prompt"],
                base_image=r["docker_image"],
            )
            for r in rows
        ]

    def version(self) -> str:
        return content_hash(ROOT, DOCKERFILE)

    def setup(self, tasks: list[PythonTask]) -> None:
        for image in sorted({t.base_image for t in tasks}):
            ensure_image(image)
        for t in tasks:
            build_image(DOCKERFILE.format(task_id=t.id, base_image=t.base_image), context=ROOT, tag=t.image)

    def run_agent(self, task: PythonTask, agent: AgentSession, env: Environment) -> None:
        agent.send(Message("user", (Text(task.prompt),)))

    def reference_solution(self, task: PythonTask) -> ReferenceSolution | None:
        code = SOLUTIONS.get(task.id)
        return ReferenceSolution("harness-written", files={"solution.py": code}) if code else None

    def grade(self, task: PythonTask, grader: Environment, env: Environment, transcript: Transcript) -> Score:
        grader.upload(ROOT, "/grader")
        # no timeout: the rest of the grade timeout
        r = grader.exec(["python", "/grader/grade.py", task.id, "--workspace", "/workspace"], timeout=None)
        return _score(r.stdout, r.stderr)

    def original_workflow(self, task: PythonTask, agent_name: str, model: str, out_dir: Path) -> OriginalResult:
        workspace = out_dir / "workspace"
        workspace.mkdir(parents=True)
        with OriginalAgentWorld(agent_name, model, task.base_image, {workspace: "/workspace"}, "/workspace", out_dir) as world:
            world.copy_in(ROOT, "/tmp/bench")
            prepare = world.exec(["sh", "-c", f"python /tmp/bench/prepare.py {task.id} --workspace /workspace && rm -rf /tmp/bench"])
            if prepare.returncode != 0:
                raise RuntimeError(f"prepare.py failed: {prepare.stderr}")
            world.run_agent(task.prompt)
            world.copy_in(ROOT, "/tmp/bench")
            r = world.exec(["python", "/tmp/bench/grade.py", task.id, "--workspace", "/workspace"])
        return OriginalResult(_score(r.stdout, r.stderr), world.cost, world.exit_status)


def _score(stdout: str, stderr: str) -> Score:
    try:
        result = json.loads(stdout)
        passed = result["passed"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise GraderError(f"unreadable grader output ({e}): stdout={stdout!r} stderr={stderr!r}") from None
    return Score(value=passed, explanation=str(result.get("details")), metadata={"grader": result, "stderr": stderr})
