# Decision: one job per trial (phases are not split into separate jobs)

**Status:** Decided; built (the queue unit is a `trials` row, `database.md` #1).

**Decision:** a trial (one task × one agent) is a single queue job. Start env,
agent, and grade run back to back against the same live world (since
2026-10-29 the grader runs in a sidecar attached to that world,
`environment-and-grading.md` #7). Phases are still tracked individually
(per-phase timestamps, `database.md` #23; logs; retries), but they are not
scheduled independently.

## What we considered

Splitting phases into separate queue jobs, e.g. an inference job
(start env → agent → save workspace snapshot) followed by a grade job that
restores the snapshot into a fresh container and runs only the grader.

Benefits it would have bought:

- `harness regrade` — re-score after fixing a grader without paying for
  the agent again
- grader-only retries when grading crashes
- separate worker pools for LLM-bound agent work vs CPU-bound grading

## How other harnesses do it

| Harness | Pattern |
|---|---|
| SWE-bench, OpenHands eval, mini-swe-agent's SWE-bench runner | **Two-phase.** Inference writes a patch per task; a separate evaluation step applies it in a fresh container and runs tests. |
| Harbor / Terminal-Bench | **Single trial.** Agent then verifier in the same container (tests copied in after the agent finishes). |
| METR (Vivaria) | **Single run.** `TaskFamily.score()` runs inside the task environment. |
| Inspect AI | **Single sample** (solver then scorer in one sandbox); `inspect score` can re-score logs only when the scorer doesn't need the sandbox. |

Rule of thumb: split when the agent's output fits in a small, portable
artifact (a patch, an answer); keep phases together when grading needs
the live environment (installed packages, running services, permissions).

## Why we didn't split

- The agent must run in the live container anyway (world setup is baked
  into the image), so only grading could ever be split off.
- Not every benchmark can be graded from a snapshot (e.g. "start a server
  on port 80"), and the harness should run any agentic benchmark.
- Splitting adds a job-kind dispatcher, snapshot storage, and cross-job
  state for benefits we don't need for the take-home.

## If we revisit

Not built (`harness regrade` included). The cheap path: add a `kind` column to the queue (`trials`: `trial` | `grade`),
snapshot the workspace after the agent phase, and let adapters opt in with
`grade_from_snapshot = True`. Benchmarks that need the live environment
keep using the single `trial` job.
