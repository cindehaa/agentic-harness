import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentic_harness.agents.fake import NullAgent, OracleAgent
from agentic_harness.benchmarks.base import Benchmark
from agentic_harness.environments import DockerEnvironment
from agentic_harness.runner import run_trial
from agentic_harness.types import Limits, Outcome, Task
from agentic_harness.worker import prepare_images


@dataclass(frozen=True)
class Check:
    task_id: str
    kind: Literal["null", "oracle"]
    ok: bool | None
    detail: str
    source: str | None = None


def validate(benchmark: Benchmark, tasks: list[Task], out_dir: Path, concurrency: int = 4,
             limits: Limits = Limits()) -> list[Check]:
    benchmark.setup(tasks)
    prepare_images(benchmark, NullAgent(), tasks)
    jobs = [(t, "null") for t in tasks] + [(t, "oracle") for t in tasks]
    with ThreadPoolExecutor(concurrency) as pool:
        checks = list(pool.map(lambda job: _check(benchmark, *job, out_dir / job[0].id / job[1], limits), jobs))
    order = {t.id: i for i, t in enumerate(tasks)}
    return sorted(checks, key=lambda c: (order[c.task_id], c.kind == "oracle"))


def _check(benchmark: Benchmark, task: Task, kind: str, attempt_dir: Path, limits: Limits) -> Check:
    if kind == "null":
        outcome = run_trial(benchmark, NullAgent(), task, limits, attempt_dir, DockerEnvironment)
        if outcome.status != "graded":
            return Check(task.id, kind, False, _not_graded(outcome))
        if not _first_message(attempt_dir):
            return Check(task.id, kind, False, "run_agent sent no (or an empty) first message")
        if outcome.score_float != 0:
            return Check(task.id, kind, False, f"doing nothing scored {outcome.score_float}: {_why(outcome)}")
        return Check(task.id, kind, True, "failed, as it should")
    solution = benchmark.reference_solution(task)
    if solution is None:
        return Check(task.id, kind, None, "no reference solution: do-nothing check only")
    agent = OracleAgent(solution)
    outcome = run_trial(benchmark, agent, task, limits, attempt_dir, DockerEnvironment)
    if outcome.status != "graded":
        return Check(task.id, kind, False, _not_graded(outcome), solution.source)
    if outcome.score_float != 1:
        script = f"; its script exited {agent.session.exit_code}: {agent.session.output.strip()[-300:]!r}" if agent.session else ""
        return Check(task.id, kind, False, f"reference solution scored {outcome.score_float}: {_why(outcome)}{script}", solution.source)
    return Check(task.id, kind, True, "passed, as it should", solution.source)


def _first_message(attempt_dir: Path) -> bool:
    transcript = json.loads((attempt_dir / "transcript.json").read_text())
    return bool(transcript) and any(c["text"].strip() if "text" in c else True for c in transcript[0]["content"])


def _not_graded(outcome: Outcome) -> str:
    lines = (outcome.error or "").strip().splitlines()
    return f"{outcome.status}: {lines[-1] if lines else ''}"


def _why(outcome: Outcome) -> str:
    return (outcome.score.explanation or "")[:300] if outcome.score else ""
