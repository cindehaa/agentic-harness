# Validating adapters, parity, and AI-written adapters

**Status:** built 2026-10-29/30: `harness validate`, `harness parity` (baseline C), the
`add-benchmark` skill, `harness adapt`, and the Terminal-Bench proof. Sections 1–2 were the design; "Built" below
records what changed while building (it supersedes 1–2 where they differ). Open: mini's `environment.env`
difference parity found; no `original_workflow` for Claude Code or Terminal-Bench.

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
  them (e.g. `benchmarks/<name>/solutions/<task_id>.sh`) — tiny for these tasks. (Built instead as
  harness-written `Benchmark.reference_solution(task)` in each adapter; see Built.)
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
3. Write `adapters/<name>.py` (+ oracle solutions if missing and feasible). (Built: `src/agentic_harness/benchmarks/<name>.py`,
   its `BENCHMARKS` entry and `tests/test_<name>.py`.)
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

### Proof: Terminal-Bench, drafted by `harness adapt` (2026-10-30)

`harness adapt benchmarks/terminal-bench --tasks 3 --model sonnet --max-budget-usd 3` wrote
`src/agentic_harness/benchmarks/terminal_bench.py`, its registry entry and `tests/test_terminal_bench.py`; validate passed
3/3 + 3/3 on its first run (commit `4d32315`, unedited).

- **Got right:** each task's own Dockerfile built via `build_image` (base images pre-pulled); the
  `task.yaml` instruction verbatim; Terminal-Bench's own grading design (tests + `run-tests.sh`
  copied into the agent's container only at grade time, `TEST_DIR=/tests`, run from the WORKDIR,
  `max_test_timeout_sec`); a faithful port of its pytest `-rA` summary parser; no summary →
  `GraderError`; a fidelity note (agent starts in `/workspace`, not `/app`; grading needs network).
- **Fixed by hand** (commit `40cf1ea`): it picked `fix-permissions`, whose solution is a
  `solution.yaml` of tmux commands it joined into one script → replaced by `log-summary` (ships
  `solution.sh`); it ran `solution.sh` through `sh -c` → now as Terminal-Bench's oracle does
  (`bash /oracle/solution.sh` from the WORKDIR); `WORKDIR = "/app"` was hard-coded → a test checks
  every adapted image's WorkingDir; the fidelity note now names the parser's two deliberate
  differences (missing summary = `grader_error` where Terminal-Bench scores a fail; empty summary =
  fail where its `all()` would pass).
- Not built: `original_workflow` (Terminal-Bench's original run is its own `tb` harness with tmux
  agents; mini has no Terminal-Bench mode), so no parity for it yet.
