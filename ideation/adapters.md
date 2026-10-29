# Ideas: validating adapters + AI-written adapters

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
original harness. Parity is a future command, out of scope (see
`task-input.md` → Fidelity).

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
