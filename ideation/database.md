# The database (Phase 4+5)

**Status:** Decided 2026-10-29. Phases 4 and 5 merged (2026-10-29): the
database, the queue, the workers and `status` / `results` are built together
in `store.py`, `worker.py` and `cli.py`.

One SQLite file, `runs/harness.db`, in WAL mode (readers don't block the one
writer, so `status` works while workers run). Only the harness writes it.

## Tables

```
runs(id, benchmark, agent, model, versions json, config json, status, setup_claimed_by, setup_lease_until, created_at, finished_at)
trials(id, run_id, task_id, state, claimed_by, lease_until, available_at, attempt_count, outcome, score_value json, score_float)
attempts(id, trial_id, n, outcome, score json, exit_status, error, cost_usd, duration_s, artifacts_dir)
events(id, attempt_id, seq, turn, kind, payload json, ts)
```

- `runs.status` is `pending` (waiting for setup) | `running` (setup done, trials claimable) | `done` (every trial done) | `failed` (setup failed).
- `runs.finished_at` (added 2026-10-29): when the run became `done` or `failed`; `harness status` lists runs finished in the last hour.
- `trials.state` is `pending` | `running` | `done`. `done` is final: the claim query only takes `pending`.
- `runs.setup_lease_until`, `trials.available_at` (added 2026-10-29, #19): the setup claim's lease; the earliest time a re-queued trial may be claimed (retry backoff).
- A `schema_version` table holds one number. Tables are created with `CREATE TABLE IF NOT EXISTS` on startup; if the stored version differs from the code's, the harness refuses to start and says to delete `runs/harness.db` (no migrations).
- `trials.outcome` / `attempts.outcome` is one of the four statuses: `graded`, `grader_error`, `infra_error`, `unsupported`.
- No `benchmarks` or `agents` tables: the registries live in code (`BENCHMARKS`, `AGENTS`).

## Decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| 1 | The queue lives in `trials` (`state`, `claimed_by`, `lease_until`) | A trial is the queue unit; one row says both "what to run" and "how it went" | A separate `jobs` table (two rows to keep in sync per trial) |
| 2 | Queue-mode setup: the first worker to claim `runs.setup_claimed_by` runs `Benchmark.setup()`, then sets the run's `status` to `running`. Trials are claimable only once their run is `running` | `setup()` (image builds) runs exactly once per run, before any trial needs the images | Every worker running setup (duplicate builds); the CLI running setup before enqueueing (`harness run` would block on builds) |
| 3 | An `attempts` table: a retry adds a row and never overwrites one. The trial holds the final outcome and score | Retries stay inspectable (why did attempt 1 fail?); queries for results read only `trials` | Overwriting the trial row on retry (loses the infra failure that caused it) |
| 4 | The score is stored raw as JSON (`score_value`) plus a converted `score_float` (Inspect-style conversion, `Benchmark.score_to_float`) | Raw keeps the grader's own verdict; the float makes accuracy one SQL `AVG` | Only a float (loses e.g. `"P"` vs `0.5`); only raw (convert on every read) |
| 5 | `events` is an agent-neutral trace of everything that passes through the harness: messages, commands + their output, lifecycle steps; grouped by `turn`. Built (2026-10-29 audit): `recording.py`: the benchmark gets a `RecordingSession` (`message_sent` / `message_received`), the agent gets its world wrapped in a `RecordingEnvironment` (one `command` event per exec: command, cwd, exit code, stdout/stderr capped at 10 000 chars, timed out, duration); both write one `EventLog` (redacted, flushed per event) | One format for every agent; reads the same whether the agent is mini or Claude Code | Storing only the agent's own trajectory format (different per agent) |
| 6 | Large files live on disk under `attempts.artifacts_dir` (`runs/<run>/<trial>/attempt-<n>/`); no artifacts table | The folder is the index; the DB stays small | An `artifacts` table listing every file |
| 7 | `runs.versions` = harness commit, agent submodule commit, benchmark dataset hash, model. `runs.config` = limits and settings | Enough to reproduce or explain a run | Not recording versions (can't tell why two runs differ) |
| 8 | SQLite + WAL for one machine. Postgres only if workers span machines (talking point, not built) | No server to run; SQLite's locking is enough for one machine's workers | Postgres now (a server for a single-machine take-home) |

## Merged Phase 4+5 decisions (2026-10-29)

| # | Decision | Why | Rejected |
|---|---|---|---|
| 9 | Phases 4 and 5 merged: `harness run` inserts the run and its trials and returns the run id at once; `harness worker --concurrency N` claims and runs trials; `harness run --wait` submits, runs an inline worker for that run, and prints its results | One code path for every trial (always through the queue); `--wait` keeps the one-command flow | A synchronous `run` plus a separate queued path (two ways to run a trial) |
| 10 | Plain `sqlite3` with handwritten SQL, all in one `store.py` (no ORM). WAL mode + `busy_timeout`, so readers never block and a busy writer waits instead of failing | Every query is visible and explainable; four tables don't need an ORM | SQLAlchemy Core + Alembic + Postgres: the scale-up path (talking point), not built |
| 11 | Atomic claim: `BEGIN IMMEDIATE`, then `UPDATE trials SET state='running', claimed_by=?, lease_until=? WHERE id=(SELECT ... WHERE state='pending' AND the run is 'running' ORDER BY ... LIMIT 1) RETURNING *` | `BEGIN IMMEDIATE` takes SQLite's write lock first, so two workers can't pick the same row | Select, then update (two workers can select the same row in between) |
| 12 | Leases: a claim holds the trial for 120 s and creates its attempt row in the same transaction; a background thread renews it every 30 s while the trial runs. An expired lease is reclaimed by any worker: the stale attempt is closed as `infra_error` ("worker lost"), which counts toward the 3 attempts, and the trial goes back to `pending` (or ends `infra_error` after 3 attempts) | A killed worker's trial is re-run without a human | No leases (a dead worker's trial stays `running` forever) |
| 13 | Queue-mode setup: the first worker to see a `pending` run claims `runs.setup_claimed_by` atomically (with a lease, `setup_lease_until`, renewed while it builds; an expired one is re-claimed by another worker), runs `Benchmark.setup(tasks)`, then sets the run `running`. If setup raises: the traceback (with the build log) is saved to `runs/<run>/setup.log`, run `failed`, every trial `done` with outcome `infra_error` (one attempt row each, error = the setup failure) | Setup runs once per run; a broken setup ends the run visibly instead of leaving it queued | Retrying setup (a broken Dockerfile fails the same way every time) |
| 14 | Auto-retry: a **retryable** `infra_error` (`Outcome.retryable`, 2026-10-29 audit; `errors.md`) is retried up to 3 attempts in total, with a short backoff: the trial goes back to `pending` with `available_at` = now + 5 s × attempt number, and any worker takes it from then. `graded`, `grader_error`, `unsupported` are never retried. After 3 failed attempts the trial ends `infra_error` | Infra failures are usually transient (Docker hiccup, provider blip); the other outcomes would repeat | Retrying every non-`graded` outcome (re-runs real grader bugs and agent limits) |
| 15 | `--concurrency N` = N worker **threads** in one process, each running one trial (one container) at a time | Trials spend their time waiting on `docker` subprocesses and model HTTP calls, so the GIL doesn't matter; threads share the loaded registries and need no pickling | Processes (more isolation, but nothing CPU-bound to gain) |
| 16 | Writes: run/trial/attempt rows are written as their state changes (so `status` is live). Trace events are appended to `<attempt_dir>/events.jsonl` as they happen and loaded into `events` at the end of each turn (option C). A worker crash loses nothing already appended. Large files stay in `attempts.artifacts_dir` | Appending a line is cheap and durable; one DB write per turn instead of per event | Writing every event to the DB (a write per event); loading only at the end (a crash loses the trace from the DB) |
| 17 | Secret redaction: before transcripts, events or files are written, every `.env` value whose key contains `KEY` / `TOKEN` / `SECRET` / `PASSWORD` and is ≥ 8 characters is replaced with `***` (`redact.py`) | Logs and the DB are safe to share | Redacting only at read time (the secret is already on disk) |
| 19 | Queue correctness (architecture audit, 2026-10-29): (a) **fenced writes**: finishing an attempt or setup, and renewing a lease, only succeed `WHERE claimed_by = me AND attempt_count = my attempt` (setup: `setup_claimed_by = me`); otherwise the worker lost its lease and its result is discarded (logged), never written over the new owner's. (b) The heartbeat retries a renewal that fails with "database is locked" (after `busy_timeout`) instead of letting the lease lapse. (c) A lost worker's attempt counts toward the 3, so a trial that crashes its worker every time ends `infra_error` (no poison trial). (d) Backoff via `trials.available_at`. (e) Setup lease (#13). (f) Each attempt's folder is named by its attempt row id, created at claim time (`runs/<run>/<task>/attempt-<attempt_id>/`), so a retry or a reclaim never reuses one. `outcome.json` / `transcript.json` are written atomically (temp file + rename); files first, then one DB transaction (outcome, remaining events, trial update) is the attempt's commit point | A worker that was only slow (laptop asleep, long GC) must not overwrite the trial another worker now owns; every failure mode ends in a final state | Trusting that a worker that finishes still owns its trial |
| 18 | `runs.versions` = harness git commit, agent submodule commit, benchmark dataset hash (sha256 of every task's fields), model. `runs.config` = the limits | Enough to reproduce or explain a run | — |

## Reading it back

- **`harness status`** (no id): active runs plus runs finished in the last
  hour (`--all` for every run): `RUN, BENCHMARK, AGENT, PROGRESS, STARTED`;
  PROGRESS is e.g. `3/5 done`, `setting up`, `queued`, `setup failed`.
- **`harness status --run_id X`**: one row per trial: `TASK, STATE, OUTCOME, SCORE, ATTEMPTS`.
- **`harness results --run_id X --format summary|table|json`**:
  - `summary` = benchmark · agent (model); accuracy = mean `score_float` over
    `graded` trials (n graded); the excluded counts `grader_error · infra_error
    · unsupported` always shown, even when 0 (with their task ids); cost; time.
  - `table` = one row per trial: task, outcome, score, cost, time, attempts.
  - `json` = everything: run (versions, config), trials with raw
    `score_value`, and their attempts.
