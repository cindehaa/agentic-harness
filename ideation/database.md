# The database (Phase 4/5)

**Status:** Decided 2026-10-29. Code not written yet (Phase 4: schema +
`status` / `results`; Phase 5: queue + workers).

One SQLite file, `runs/harness.db`, in WAL mode (readers don't block the one
writer, so `status` works while workers run). Only the harness writes it.

## Tables

```
runs(id, benchmark, agent, model, versions json, config json, status, setup_claimed_by, created_at)
trials(id, run_id, task_id, state, claimed_by, lease_until, attempt_count, outcome, score_value json, score_float)
attempts(id, trial_id, n, outcome, score json, exit_status, error, cost_usd, duration_s, artifacts_dir)
events(id, attempt_id, seq, turn, kind, payload json, ts)
```

- `trials.state` is `pending` | `running` | `done`.
- `trials.outcome` / `attempts.outcome` is one of the four statuses: `graded`, `grader_error`, `infra_error`, `unsupported`.
- No `benchmarks` or `agents` tables: the registries live in code (`BENCHMARKS`, `AGENTS`).

## Decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| 1 | The queue lives in `trials` (`state`, `claimed_by`, `lease_until`) | A trial is the queue unit; one row says both "what to run" and "how it went" | A separate `jobs` table (two rows to keep in sync per trial) |
| 2 | Queue-mode setup: the first worker to claim `runs.setup_claimed_by` runs `Benchmark.setup()`, then sets the run's `status` to `running`. Trials are claimable only once their run is `running` | `setup()` (image builds) runs exactly once per run, before any trial needs the images | Every worker running setup (duplicate builds); the CLI running setup before enqueueing (`harness run` would block on builds) |
| 3 | An `attempts` table: a retry adds a row and never overwrites one. The trial holds the final outcome and score | Retries stay inspectable (why did attempt 1 fail?); queries for results read only `trials` | Overwriting the trial row on retry (loses the infra failure that caused it) |
| 4 | The score is stored raw as JSON (`score_value`) plus a converted `score_float` (Inspect-style conversion, `Benchmark.score_to_float`) | Raw keeps the grader's own verdict; the float makes accuracy one SQL `AVG` | Only a float (loses e.g. `"P"` vs `0.5`); only raw (convert on every read) |
| 5 | `events` is an agent-neutral trace of everything that passes through the harness: messages, commands + their output, lifecycle steps; grouped by `turn` | One format for every agent; reads the same whether the agent is mini or Claude Code | Storing only the agent's own trajectory format (different per agent) |
| 6 | Large files live on disk under `attempts.artifacts_dir` (`runs/<run>/<trial>/attempt-<n>/`); no artifacts table | The folder is the index; the DB stays small | An `artifacts` table listing every file |
| 7 | `runs.versions` = harness commit, agent submodule commit, benchmark dataset hash, model. `runs.config` = limits and settings | Enough to reproduce or explain a run | Not recording versions (can't tell why two runs differ) |
| 8 | SQLite + WAL for one machine. Postgres only if workers span machines (talking point, not built) | No server to run; SQLite's locking is enough for one machine's workers | Postgres now (a server for a single-machine take-home) |

## Reading it back

- **`harness status`** = counts of trials by `state` and by `outcome`.
- **`harness results`** = accuracy = mean `score_float` over `graded` trials;
  `grader_error`, `infra_error` and `unsupported` trials are listed separately
  (see the Outcome model in `task-input.md`).
