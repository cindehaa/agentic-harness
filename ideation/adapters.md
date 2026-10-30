# Validating adapters, parity, and AI-written adapters

**Status:** built 2026-10-29: `harness validate`, `harness parity` (baseline C), the
`add-benchmark` skill and `harness adapt`. Sections 1–2 were the design; "Built" below records
what changed while building.

Both are optional extensions on top of the core harness. They build on the
`Benchmark` adapter interface (`tasks`, `setup`, `run_agent`, `grade`) and answer the
problem statement's "integration overhead" point: adapters are still written
once per benchmark, but they become cheap to write and safe to trust.

## 1. `harness validate --benchmark <name>`

Checks that an adapter is wired correctly without running a real agent.
Runs every task through the normal trial pipeline, swapping the agent for
built-in fake agents:

| Check | Fake agent | Expected | Catches |
|---|---|---|---|
| Null run | does nothing | every task **fails** | grader that always passes, setup that leaks the answer |
| Oracle run | applies the reference solution | every task **passes** | broken setup, wrong paths, wrong pass/fail parsing |
| Listing | — | every task has `benchmark`, `id`, `image`; `run_agent` sends a non-empty first message | bad field mapping (`task_id` vs `id`, `problem_statement` vs `prompt`) |
| Image check | — | image pulls, required tools exist (e.g. python for in-container grading) | alpine-has-no-python style surprises |

- Neither sample benchmark ships solutions, so we write oracle solutions for
  them (e.g. `benchmarks/<name>/solutions/<task_id>.sh`) — tiny for these tasks.
  Benchmarks without solutions get the null check only, reported as such.
- Output: per-task pass/fail table plus a summary; non-zero exit if any check
  fails so it can run in CI.
- Implementation stays small because it reuses the trial runner: `NullAgent`
  and `OracleAgent` are just two more agent adapters.

Prior art: Harbor tasks include a reference solution and the CLI has
`harbor adapters validate`; SWE-bench validates instances by checking that
tests fail before and pass after the gold patch.

`validate` is not the same as `harness parity`. `validate` checks wiring with
the null and oracle agents. `harness parity` checks that scores match the
original workflow (built 2026-10-29; see "Built" below).

## 2. AI-written adapters ("add a benchmark" skill)

A Claude Code skill (`.claude/skills/add-benchmark/SKILL.md`) that drafts a
new adapter, with `harness validate` as the gate:

1. Read the new benchmark's README, dataset and scripts.
2. Read the `Benchmark` base class and one existing adapter as the example.
3. Write `adapters/<name>.py` (+ oracle solutions if missing and feasible).
4. Run `harness validate --benchmark <name>`; fix and repeat until it passes.
5. Report what it assumed (how pass/fail is read, what the grader needs installed).

The point to make: the generation is easy and not novel; the **validator is
what makes generated adapters trustworthy**. A human still reviews the diff.

Demo idea for the onsite: delete the python-tasks adapter, run the skill,
show it regenerating one that passes validation.

Prior art: Harbor publishes an agent-facing adapter guide plus
`harbor adapters init` / `validate`; community skills exist for authoring
Inspect AI tasks. Nothing found that has an agent fully generate adapters
on its own (searched 2026-10-28).

## Sources

- [Harbor Adapters paper](https://arxiv.org/pdf/2609.04298)
- [Harbor adapter agent guide](https://harborframework.com/docs/adapters)
- [Harbor skill (tessl.io)](https://tessl.io/registry/honeybadge/harbor/skills)
- [A²E — Agent Task Protocol](https://arxiv.org/pdf/2608.07346)
- [Inspect Eval Author skill](https://claudskills.com/skills/inspect-eval-author/)

## Built (2026-10-29)

### `harness validate --benchmark X [--task_ids …]`

- Two fake agents (`agents/fake.py`, not in `AGENTS`) go through the normal `run_trial`:
  `NullAgent` (replies nothing) must leave every task failing; `OracleAgent` applies
  `Benchmark.reference_solution(task)` and must pass it (`score_float == 1`). The listing check is
  folded into the null run: the benchmark's first message must be non-empty. The image check is
  implicit (setup + pre-pull run first; a failure shows as `infra_error`).
- `ReferenceSolution(source, files, script, cwd)` in `types.py`. `source` is
  `"benchmark-provided"` (shipped by the benchmark, e.g. Terminal-Bench `solution.sh`: an
  independent check) or `"harness-written"` (ours: checks the wiring only). The old test oracles of
  bash-operations / python-tasks moved into their adapters as harness-written solutions. Tasks
  without one get the null check only, reported as such.
- Per-task table + summary; artifacts in `runs/validate/<benchmark>-<ts>/<task>/<null|oracle>/`;
  exit 1 on any failed check.

### `harness parity --benchmark X --agent Y --runs N [--task_ids …]`

Parity is no longer "future": built with **baseline C**. The original side runs the benchmark's
own setup + grader scripts unchanged and the agent in its **own** Docker mode on the image the
benchmark declares; for mini-swe-agent that is mini's `DockerEnvironment` + `DefaultAgent` with its
own `mini.yaml` (`agentic_harness/original.py`), a host folder bind-mounted as the workspace like
the starter's examples. Same model and step / cost / command limits as a harness trial, so the only
difference is our harness (adapter, prompt delivery, grader sidecar, runner). Nothing the agent does
runs on the host; the benchmark's scripts run in containers too (bash-operations: `setup.py` /
`evaluate.py` in `python:3.11-slim`; python-tasks: `prepare.py` / `grade.py` inside mini's own
container, copied in only to run).

- `Benchmark.original_workflow(task, agent_name, model, out_dir) -> OriginalResult(score,
  cost_usd, agent_exit_status)`; default raises `NotImplementedError`. Agents other than
  mini-swe-agent have no original mode yet.
- Report: per side mean ± sample SEM of run accuracies, the run-score ranges and whether they
  overlap (Harbor's rule: no overlap = adaptation error until shown otherwise), per-task pass
  rates, cost; `runs/parity/<benchmark>-<ts>/parity.json`. An original run that crashes is left
  out and listed (like `infra_error`).
- Live, 2026-10-29, bash-operations × mini-swe-agent (Haiku 5.5), 3 runs per side: harness
  1.000 ± 0.000, original 1.000 ± 0.000, ranges overlap; every task 1.00 on both sides; cost
  $0.0069 + $0.0080.
- Differences parity exposed while building (not fixed; for Cindy): our `MiniSweAgent` drops
  `mini.yaml`'s `environment.env` (`PAGER=cat`, `MANPAGER=cat`, `LESS=-R`, `PIP_PROGRESS_BAR=off`,
  `TQDM_DISABLE=1`), which mini's own environments set; mini's own `DockerEnvironment` reports the
  host's OS (macOS) in its prompt where ours reports the container's; mini's default shell is
  `bash -lc` (falls back to `sh -lc` on alpine) where ours is `sh -c`.

### Skill + `harness adapt`

- `.claude/skills/add-benchmark/SKILL.md` is the single source of the adapter rules (reuse the
  benchmark's scripts, verbatim wording, typed Task, no dependency picking, grader files never in
  the world early), the methods, register + test, and the validate loop.
- `harness adapt <benchmark-dir> [--tasks N] [--name] [--model] [--max-budget-usd]` runs
  `claude -p` on the host with the skill as the prompt (on stdin), `--permission-mode acceptEdits`
  (edits inside this repo only), a tool allowlist (read/edit, `harness validate`, pytest, read-only
  docker; no git, no web) and a spend cap, then runs `harness validate` in a fresh process. A
  drafted adapter needs human review.
