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

## Four designs

| Design | Who uses it | Integrity benefit | Fidelity cost |
|---|---|---|---|
| Late copy: tests copied in only at grade time | Harbor (`tests/`), SWE-bench (test patch) | Agent never sees or edits the tests | High here: the benchmark puts the tests in the workspace on purpose (agent can read and run them) |
| Read-only tests via a non-root agent user | METR | Agent sees but can't edit | Medium: changes the agent's user and permissions; tasks may expect root |
| Grade a clean copy (fresh tests over the agent's solution) | SWE-bench | Edits to tests are ignored | Medium: harness decides which files are "solution" vs "tests", overriding the benchmark's grader input |
| Detect, don't prevent | (ours) | Tampering is visible, not blocked | None: world, prompt, grader and score unchanged |

## Decision

- **Adapters stay faithful.** No restoring tests, no prompt changes.
- **The harness adds benchmark-neutral detection.** `Benchmark.protected_files(task)`
  (default `[]`) names the files that matter for grading; `integrity.py`
  hashes them before and after the agent's turn. The trial outcome gets
  `modified_protected_files`; the score is never altered.
- Late copy / read-only tests remain tools for benchmarks designed for our
  harness, where they are the benchmark's design rather than a change to it.

## Quirks kept as-is

Benchmark quirks are part of the benchmark. E.g. bash-003's `input.txt` has no
trailing newline, so `wc -l` gives 4, not 5; a `wc -l` answer fails. We don't
fix it.
