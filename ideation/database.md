# The database (Phase 4+5)

**Status:** Decided and built 2026-10-29. Phases 4 and 5 merged (2026-10-29): the
database, the queue, the workers and `status` / `results` are built together
in `store.py`, `worker.py` and `cli.py`. Schema version 4 (cost + time tracking, #20–23).

One SQLite file, `runs/harness.db`, in WAL mode (readers don't block the one
writer, so `status` works while workers run). Only the harness writes it.

## Tables

```
runs(id, benchmark, agent, model, versions json, config json, max_cost_usd, status, setup_claimed_by, setup_lease_until,
     setup_started_at, setup_finished_at, created_at, finished_at)
trials(id, run_id, task_id, state, claimed_by, lease_until, available_at, attempt_count, outcome, score_value json, score_float)
attempts(id, trial_id, n, outcome, score json, exit_status, error, cost_usd, model_calls, input_tokens, output_tokens,
         duration_s, artifacts_dir, queued_at, started_at, env_ready_at, agent_start_at, agent_end_at, grade_start_at,
         grade_end_at, finished_at)
events(id, attempt_id, seq, turn, kind, payload json, ts)
workers(id, started_at, heartbeat_at)
```

- `runs.status` is `pending` (waiting for setup) | `running` (setup done, trials claimable) | `done` (every trial done) | `failed` (setup failed).
- `runs.finished_at` (added 2026-10-29): when the run became `done` or `failed`; `harness status` lists runs finished in the last hour.
- `trials.state` is `pending` | `running` | `done`.
- Names (2026-10-29 audit): the **trace** = the agent-neutral events (`events.jsonl` on disk, the `events` table in the DB); a **trajectory** = an agent's own log in its own format (mini's `trajectory.json`), kept as a file in the attempt folder; the **transcript** = the messages only (`transcript.json`, what `grade()` sees).
- `workers` (2026-10-29 audit, schema version 2): one row per `harness worker` process (`host-pid`), heartbeat every 30 s, deleted on exit. `harness status` ends with a warning, e.g. "3 pending — no worker running; start one with `harness worker`", when trials of unfinished runs are not done and no heartbeat is newer than the 120 s lease (2026-10-29; a running trial counts: its lease lapses with its worker). `done` is final: the claim query only takes `pending`.
- `runs.setup_lease_until`, `trials.available_at` (added 2026-10-29, #19): the setup claim's lease; the earliest time a re-queued trial may be claimed (retry backoff).
- A `schema_version` table holds one number. Tables are created with `CREATE TABLE IF NOT EXISTS` on startup; if the stored version differs from the code's, the harness refuses to start and says to delete `runs/harness.db` (no migrations).
- `trials.outcome` / `attempts.outcome` is one of the four statuses: `graded`, `grader_error`, `infra_error`, `unsupported`; a trial's can also be `skipped_budget` (#22; never an attempt's: such a trial has none).
- **Schema version 4** (2026-10-29, cost + time tracking, #20–23): `attempts` gains the agent's spend (`model_calls`, `input_tokens`, `output_tokens`; `cost_usd` existed) and its phase boundaries (`queued_at`, `env_ready_at`, `agent_start_at`, `agent_end_at`, `grade_start_at`, `grade_end_at`; `started_at` = claimed and `finished_at` came with v3); `runs` gains `max_cost_usd`, `setup_started_at`, `setup_finished_at`. All times are `time.time()`; a phase an attempt never reached stays NULL.
- **Schema version 5** (2026-10-31, limits in three layers, `agents-and-runner.md` L1–L7): `attempts.limits` (json:
  each limit's effective value, its source `default` / `benchmark` / `flag`, and `enforced: false` for one the agent
  can't keep). An older `harness.db` is refused, not migrated: delete it.
- No `benchmarks` or `agents` tables: the registries live in code (`BENCHMARKS`, `AGENTS`).

## Decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| 1 | The queue lives in `trials` (`state`, `claimed_by`, `lease_until`) | A trial is the queue unit; one row says both "what to run" and "how it went" | A separate `jobs` table (two rows to keep in sync per trial) |
| 2 | Queue-mode setup: the first worker to claim `runs.setup_claimed_by` runs `Benchmark.setup()`, then sets the run's `status` to `running`. Trials are claimable only once their run is `running` | `setup()` (image builds) runs exactly once per run, before any trial needs the images | Every worker running setup (duplicate builds); the CLI running setup before enqueueing (`harness run` would block on builds) |
| 3 | An `attempts` table: a retry adds a row and never overwrites one. The trial holds the final outcome and score | Retries stay inspectable (why did attempt 1 fail?); queries for results read only `trials` | Overwriting the trial row on retry (loses the infra failure that caused it) |
| 4 | The score is stored raw as JSON (`score_value`) plus a converted `score_float` (Inspect-style conversion, `Benchmark.score_to_float`) | Raw keeps the grader's own verdict; the float makes accuracy one SQL `AVG` | Only a float (loses e.g. `"P"` vs `0.5`); only raw (convert on every read) |
| 5 | `events` is an agent-neutral trace of everything that passes through the harness: messages, commands + their output, lifecycle steps; grouped by `turn`. Built (2026-10-29 audit): `recording.py`: the benchmark gets a `RecordingSession` (`message_sent` / `message_received`), the agent gets its world wrapped in a `RecordingEnvironment` (one `command` event per exec: command, cwd, exit code, stdout/stderr capped at 10 000 chars, timed out, duration); both write one `EventLog` (redacted, flushed per event) | One format for every agent; reads the same whether the agent is mini or Claude Code | Storing only the agent's own trajectory format (different per agent) |
| 6 | Large files live on disk under `attempts.artifacts_dir` (`runs/<run>/<trial>/attempt-<n>/`; since #19f `attempt-<attempt_id>`); no artifacts table | The folder is the index; the DB stays small | An `artifacts` table listing every file |
| 7 | `runs.versions` = harness commit, agent submodule commit, benchmark dataset hash, model (refined in #18). `runs.config` = limits and settings | Enough to reproduce or explain a run | Not recording versions (can't tell why two runs differ) |
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
| 15 | `--concurrency N` = N worker **threads** in one process, each running one trial (one container) at a time. Global cap (2026-10-30): `harness worker --max-running N` (or `HARNESS_MAX_RUNNING`; also `run --wait`): the claim's write transaction also checks that fewer than N trials are `running` with an unexpired lease, across every worker | Trials spend their time waiting on `docker` subprocesses and model HTTP calls, so the GIL doesn't matter; threads share the loaded registries and need no pickling | Processes (more isolation, but nothing CPU-bound to gain) |
| 16 | Writes: run/trial/attempt rows are written as their state changes (so `status` is live). Trace events are appended to `<attempt_dir>/events.jsonl` as they happen and loaded into `events` at the end of each turn (option C). A worker crash loses nothing already appended. Large files stay in `attempts.artifacts_dir` | Appending a line is cheap and durable; one DB write per turn instead of per event | Writing every event to the DB (a write per event); loading only at the end (a crash loses the trace from the DB) |
| 17 | Secret redaction: before transcripts, events or files are written, every `.env` value whose key contains `KEY` / `TOKEN` / `SECRET` / `PASSWORD` and is ≥ 8 characters is replaced with `***` (`redact.py`). 2026-10-30: also secret-named variables in the harness's own environment (same rule) and every variable an agent forwards into its worlds (`Agent.forward_env`, any name, ≥ 8 characters) | Logs and the DB are safe to share | Redacting only at read time (the secret is already on disk) |
| 19 | Queue correctness (architecture audit, 2026-10-29): (a) **fenced writes**: finishing an attempt or setup, and renewing a lease, only succeed `WHERE claimed_by = me AND attempt_count = my attempt` (setup: `setup_claimed_by = me`); otherwise the worker lost its lease and its result is discarded (logged), never written over the new owner's. (b) The heartbeat retries a renewal that fails with "database is locked" (after `busy_timeout`) instead of letting the lease lapse. (c) A lost worker's attempt counts toward the 3, so a trial that crashes its worker every time ends `infra_error` (no poison trial). (d) Backoff via `trials.available_at`. (e) Setup lease (#13). (f) Each attempt's folder is named by its attempt row id, created at claim time (`runs/<run>/<task>/attempt-<attempt_id>/`), so a retry or a reclaim never reuses one. `outcome.json` / `transcript.json` are written atomically (temp file + rename); files first, then one DB transaction (outcome, remaining events, trial update) is the attempt's commit point | A worker that was only slow (laptop asleep, long GC) must not overwrite the trial another worker now owns; every failure mode ends in a final state | Trusting that a worker that finishes still owns its trial |
| 18 | `runs.versions` = harness git commit, agent version (`Agent.version()`: mini's submodule commit), benchmark dataset hash (sha256 of every task's fields), benchmark content hash (`Benchmark.version()`: its folder + Dockerfile template, also in its image tags, e.g. `harness-bash-operations:bash-001-<12 hex>`; 2026-10-29 audit), model. `runs.config` = the limits (2026-10-31: `limits` = harness defaults, `limit_flags` = the flags given, `non_standard_limits`; schema v5 adds `attempts.limits`: each attempt's effective limits with their source, `agents-and-runner.md` L1–L5) | Enough to reproduce or explain a run | — |

## Cost and time tracking (2026-10-29, schema v4)

| # | Decision | Why | Rejected |
|---|---|---|---|
| 20 | Agents report `cost_usd`, `model_calls`, `input_tokens`, `output_tokens` in `AgentSession.stats()` (`AgentStats`). mini: its own `cost` / `n_calls`, tokens summed from litellm's `usage` on each model message. ACP agents (2026-10-30, was Claude Code's stream-json result event): cost = the last `usage_update.cost.amount` (the session's total), tokens = each turn's `PromptResponse.usage`; tokens but no cost → priced with litellm; `model_calls` = 0 (ACP reports none). Input tokens include cached prompt tokens (litellm's `prompt_tokens` does; Anthropic's `input_tokens` doesn't, so the cached-read and cached-write counts are added) | One shape for every agent; tokens explain cost differences between models and agents | Pricing tokens ourselves for mini (litellm already does); reading only Claude Code's result event (a run cut off at the deadline would cost $0) |
| 21 | Spend is always recorded: `RecordingSession.send` reports `stats()` to the worker in a `finally` at every turn's end (also a turn the agent crashed or timed out in), and the worker writes it at once (`Store.record_spend`), then again after the attempt (whatever its outcome, also if interrupted or its claim was lost). Unfenced (an attempt row is that attempt's own; money spent before a lost claim was still spent) and `MAX`ed (an outcome without stats, e.g. "worker interrupted", never erases it) | Money spent is spent: a crash, a timeout or Ctrl-C must not make a run look cheaper, and the budget (#22) needs live spend | Recording cost only with a finished attempt's outcome (a crashed or interrupted attempt's spend was lost) |
| 22 | Run budget: `harness run --max-run-cost USD` → `runs.max_cost_usd`. The claim query skips a run whose attempts' recorded spend has reached it; whenever spend is recorded or a trial ends, a run over budget has every `pending` trial (a retry in its backoff too) set `done` with outcome `skipped_budget` (no attempt row: it never started), then the run is `done` once nothing is in flight. Trials in flight finish within their own per-trial limit, so a run can overshoot by what they spend. `skipped_budget` is excluded from accuracy and reported like the other excluded outcomes | A clear final state for every trial: the run finishes and says which tasks have no score and why | Leaving them `pending` (the run never finishes); `infra_error` (not infra's fault, not retryable); killing trials in flight (their spend is already sunk, their result would be lost) |
| 23 | Time: each attempt records phase boundaries: `queued_at` (its trial became claimable: the later of setup done and the retry backoff's end), `started_at` (claimed), `env_ready_at` (world + sidecars up), `agent_start_at` / `agent_end_at` (the benchmark's turns), `grade_start_at` / `grade_end_at` (kill leftovers, save workspace, grader, `grade()`), `finished_at` (outcome recorded). Durations are derived: queue wait = started − queued, env start = env_ready − started, agent, grading, total. Run level: setup = `setup_finished_at − setup_started_at`; wall = first attempt start → last attempt end (now, while running); queue wait summed separately; throughput = trials run / wall minutes. Computed in `metrics.py` from the rows | Timestamps, not durations, so any breakdown can be derived later and they can be checked for order; queue wait from claimable (not submission) so setup time isn't counted twice | Storing durations (can't be re-derived or cross-checked); wall from submission (a run queued for an hour didn't take an hour) |

## Reading it back

- **`harness status`** (no id): active runs plus runs finished in the last
  hour (`--all` for every run): `RUN, BENCHMARK, AGENT, PROGRESS, SPEND, ELAPSED, STARTED`;
  PROGRESS is e.g. `3/5 done`, `3/5 done (budget reached: 2 skipped)`, `setting up`, `queued`, `setup failed`;
  SPEND is the live spend (recorded at each turn's end), `of $X` with a budget; ELAPSED is since submission.
- **`harness status --run_id X`**: the run's spend and elapsed time, then one row per trial: `TASK, STATE, OUTCOME, SCORE, ATTEMPTS, COST, TIME` (TIME includes the attempt in flight so far).
- **`harness results --run_id X --format summary|table|json`** (default `summary`, 2026-10-29):
  - `summary` = benchmark · agent (model); accuracy = mean `score_float` over
    `graded` trials (n graded, how many passed = full marks); the excluded counts `grader_error · infra_error
    · unsupported · skipped_budget` always shown, even when 0 (with their task ids); cost (of the budget),
    cost per task (trials that started) and per passing task; tokens in/out and model calls; average env start,
    agent and grading time per attempt; setup time, wall time (first attempt start → last end), total queue
    wait, throughput (#23).
  - `table` = one row per trial: task, outcome, score, cost, tokens in/out, agent time, grading time, total time, attempts.
  - `json` = everything: run (versions, config, budget), every metric of the summary, trials with raw
    `score_value`, and their attempts with every timestamp.
