---
name: add-benchmark
description: Write a benchmark adapter for this harness (a Benchmark subclass in src/agentic_harness/benchmarks/), then iterate until `harness validate` passes. Use when asked to add, adapt, or port a benchmark.
---

# Add a benchmark adapter

An adapter plugs one benchmark into the harness. It **only moves things**: the benchmark's own setup
builds the world, its own grader judges, and the agent hears only the benchmark's own words. The
adapter never improves, fixes or hardens the benchmark. These rules are the single source; the two
worked examples follow them:

- `src/agentic_harness/benchmarks/bash_operations.py` (alpine world, grader in a python sidecar)
- `src/agentic_harness/benchmarks/python_tasks.py` (world image has the grader's tools)

Read both, plus `src/agentic_harness/benchmarks/base.py` (the interface) and
`src/agentic_harness/types.py`, before writing anything.

## The rules

1. **Reuse the benchmark's own setup and grader scripts**, unchanged. Don't reimplement them, don't
   patch them, don't "fix" quirks (a task whose expected answer is surprising stays surprising).
   If a piece only exists inside the benchmark's own harness code (e.g. an output parser), port the
   smallest piece faithfully and say so in the fidelity note.
2. **Send the benchmark's wording verbatim.** The first message is exactly its instruction /
   problem statement / prompt field. The harness adds nothing: no "You are...", no paths, no hints.
3. **Typed Task subclass** (`@dataclass(frozen=True)`, subclass of `Task`): keep the benchmark's
   native fields you need (the instruction, the base image, timeouts...) under readable names.
   `id` is the benchmark's own task id; `image` is the image the world starts from.
4. **No dependency picking.** Don't `apt-get`/`apk add`/`pip install` anything the benchmark does
   not install itself. If the grader needs tools the world lacks, grade in a sidecar image that has
   them (`grader_image`), never by adding tools to the agent's world.
5. **Grader files never enter the agent's world before grading.** Upload them into the grader
   sidecar; or, if the benchmark's own design copies its tests into the agent's container after the
   agent finishes (Terminal-Bench, Harbor), do exactly that in `grade()` via `env`, at grade time.
6. **Hidden answers stay hidden**: never bake solutions, expected outputs or tests into the world
   image unless the benchmark itself puts them there.

## The methods (see `benchmarks/base.py` for signatures)

| Method | What it does |
|---|---|
| `name` | registry key, kebab-case, e.g. `"terminal-bench"` |
| `tasks()` | every task, read from the benchmark's own files (dataset JSON, task folders...) |
| `setup(tasks)` | once per run: `ensure_image(...)` for every base and grader image (pre-pull, time-bounded), then `build_image(dockerfile_text, context=folder, tag=task.image)` per task. Use the benchmark's own Dockerfile when it ships one; otherwise a small multi-stage Dockerfile that runs its setup script (see bash_operations.py). Tag images `harness-<name>:<task id>-<self.version()>`. |
| `version()` | `content_hash(folder, DOCKERFILE_TEMPLATE)`: changes whenever anything that builds images changes |
| `run_agent(task, agent, env)` | usually one line: `agent.send(Message("user", (Text(task.instruction),)))` |
| `grader_image(task)` | the image grading runs in, attached to the world (shares `/workspace`, network, processes). Default: the task's own image. |
| `grade(task, grader, env, transcript)` | run the benchmark's own grader; return `Score(value=passed, explanation=..., metadata=...)`. Raise `GraderError` when its output can't be read (never return False for "couldn't parse"). |
| `mcp_servers(task)` | optional: only if the benchmark serves its own tools to the agent |
| `reference_solution(task)` | `ReferenceSolution(source, files={path: text}, script="...", cwd=...)`. If the benchmark ships solutions, use them unchanged with `source="benchmark-provided"`; otherwise write tiny correct ones with `source="harness-written"`, or return None. |
| `original_workflow(task, agent_name, model, out_dir)` | optional, for `harness parity`: the benchmark's own scripts + the agent in its own Docker mode (`agentic_harness.original.OriginalAgentWorld`, `run_script`). Leave it out if unsure; say so. |

Facts about the world you need:

- The world is a Docker container from `task.image` whose working directory is `/workspace`, a
  fresh volume. Agents start there. If the benchmark expects another working directory (its
  Dockerfile's `WORKDIR`), run its scripts there with `cwd=` / `ReferenceSolution.cwd`, and list
  "agent starts in /workspace, not <workdir>" in the fidelity note.
- `env.exec(str)` runs through `sh -c`; `env.exec(list)` runs directly. `timeout=` in seconds
  (default 60): pass the benchmark's own timeouts where it has them.
- `env.upload(local_path, remote)`, `env.write_file(path, text)`, `env.read_file(path)`.
- Containers run with `--memory 2g --cpus 1` and network access.

## Register and test

1. `src/agentic_harness/benchmarks/__init__.py`: import the class and add it to `BENCHMARKS`.
2. `tests/test_<name>.py`, modelled on `tests/test_bash_operations.py`: tasks match the benchmark's
   files; `run_agent` sends the instruction verbatim; the reference solution passes and doing
   nothing fails (real Docker; skip the module if Docker isn't running); no leftover containers.
   Keep tests to the tasks you adapted.
3. A short fidelity note in the adapter's module docstring: what is reused, what (if anything) was
   ported, and the benchmark's quirks kept as they are.

## Iterate until validate passes

```
.venv/bin/harness validate --benchmark <name> [--task_ids a,b,c]
```

It runs every task twice through the real trial runner: a do-nothing agent (every task must FAIL)
and an oracle applying `reference_solution` (every task must PASS). Read the per-task details and
the artifacts folder it prints (`events.jsonl`, `outcome.json`). Typical causes:

| Symptom | Usual cause |
|---|---|
| do-nothing passes | the world already holds the answer, or the grader output is misread (e.g. exit code vs JSON) |
| reference fails, script exited non-zero | wrong working directory, missing files in the image, solution needs the network |
| `grader_error` | grader output not where/what you parse; print it in the error |
| `infra_error` | image build/pull failed, or a timeout too short for the benchmark's own scripts |

Then `.venv/bin/python -m pytest -q tests/test_<name>.py`. Never weaken a check or edit the
benchmark's files to make validate pass; if something can't be faithful, stop and report it.

## Report

End with: the files you wrote, the validate table, what you assumed (how pass/fail is read, what
the grader needs, working directory, network), and anything you could not make faithful.
Adapters drafted this way **need human review** before their scores are trusted.
