# Fidelity vs integrity

**Status:** Decided 2026-10-29.

## The loophole

In python-tasks, `prepare.py` writes `test_solution.py` into the agent's
workspace and `grade.py` runs whatever is there. An agent can overwrite the
tests (`def test_ok(): assert True`) and pass. The starter's own workflow
(`examples/run_python_example.sh`: prepare → agent in the workspace → grade)
has the same hole, so it is the benchmark's design, not an adapter bug.

**Fidelity cost** = how much a design changes the benchmark (what the agent
sees, what the grader runs). We want it low: a harness that quietly changes
benchmarks produces numbers nobody can compare.

## Designs

| Design | Who uses it | Integrity benefit | Fidelity cost |
|---|---|---|---|
| Late copy: tests copied in only at grade time | Harbor (`tests/`), SWE-bench (test patch) | Agent never sees or edits the tests | High here: the benchmark puts the tests in the workspace on purpose (agent can read and run them) |
| Read-only tests via a non-root agent user | METR | Agent sees but can't edit | Medium: changes the agent's user and permissions; tasks may expect root |
| Grade a clean copy (fresh tests over the agent's solution) | SWE-bench | Edits to tests are ignored | Medium: harness decides which files are "solution" vs "tests", overriding the benchmark's grader input |
| Detect with a special mechanism (`protected_files`: hash declared files before/after the agent's turn) | (ours, built then removed) | Tampering with declared files is flagged | None to the world, but the adapter must guess which files matter |
| Evidence only: trace + saved final workspace | (ours) | Every command and the end state are on record; tampering is visible on review | None: world, prompt, grader and score unchanged |

## Decision

Principle: **preserve the benchmark's own prevention; never add prevention
silently.**

- **Adapters stay faithful.** No restoring tests, no prompt changes. If a
  benchmark prevents tampering itself (e.g. late-copied tests), we keep that.
- **Integrity evidence = the trace + the saved final workspace.** The harness
  records both for every trial anyway: every command the agent ran (trace →
  events) and the final workspace (Phase 4 recorder). A workspace diff against
  the task image can be computed later if needed. No special mechanism.
- Late copy / read-only tests remain tools for benchmarks designed for our
  harness, where they are the benchmark's design rather than a change to it.

## Rejected: `protected_files` (built, then removed 2026-10-29)

`Benchmark.protected_files(task)` named the files that matter for grading;
`integrity.py` hashed them before and after the agent's turn and flagged
changes (scores unchanged). Removed as overfitting: a benchmark-specific
mechanism inside the harness, and declared files are too narrow to catch
tampering anyway:

- **`conftest.py`**: an agent can leave `test_solution.py` untouched and add a
  `conftest.py` that makes every test pass. No declared file changed.
- **Hard-coded outputs**: a "solution" that returns the exact values the tests
  check, without solving the task. Only the solution file changed, which is
  supposed to change.

Both show up in the trace and the final workspace; neither shows up in a hash
of declared files.

## Quirks kept as-is

Benchmark quirks are part of the benchmark. E.g. bash-003's `input.txt` has no
trailing newline, so `wc -l` gives 4, not 5; a `wc -l` answer fails. We don't
fix it.
