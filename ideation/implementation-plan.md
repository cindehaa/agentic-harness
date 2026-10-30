# Implementation plan

Each phase ends with something runnable and a few "explain-back" questions —
if you can't answer them out loud, the phase isn't done. Ownership says who
writes the first draft: **You** = you write it, Claude reviews;
**Claude** = Claude drafts a small diff, you review/run/commit it.

Rule of thumb for ownership: you write the parts an interviewer will poke at
(interfaces, trial runner, claim query, error semantics); Claude drafts
plumbing (docker CLI calls, CLI formatting, boilerplate SQL, tests).

---

## Phase 0 — Get oriented (no harness code)

- Commit the untouched starter code as a baseline so later diffs show only our work.
- `make install`, configure `mini-extra config setup`, run `make bash-example` and `make py-example` by hand.
- Read `benchmarks/*/setup.py|prepare.py|evaluate.py|grade.py` and mini-swe-agent's `environments/docker.py`.
- `docker run --rm -it alpine:latest sh` — confirm there's no python.

**Done when:** both examples pass locally.
**Explain-back:** How does each benchmark report pass/fail? Why can't `evaluate.py` run inside the alpine container? How does mini-swe-agent's DockerEnvironment execute a command?

## Phase 1 — Core types + Docker environment + benchmark adapters

Changed 2026-10-29: no `LocalEnvironment`; Docker starts in ~1s and a fake local world behaves differently (python on the host, path mapping), so tests use `DockerEnvironment` directly (needs Docker running).

| Piece | Owner |
|---|---|
| `Task`, `Score`, `Benchmark` ABC, registry | You |
| `Environment` ABC | Claude |
| `DockerEnvironment` (image in constructor): start, exec(cmd, timeout), copy in/out, stop | Claude |
| Workspace lives in the container (no bind mount; `download()` gets it out). Grading always runs in the trial container, after the agent | You |
| Guaranteed cleanup (`finally`, container labels, `harness cleanup`) | Claude |
| `BashOperations`, `PythonTasks` adapters (world setup, incl. python-tasks' `prepare.py`, baked into images in `Benchmark.setup()`) | You (first), Claude (second, for comparison) |
| Hand-written oracle solutions + pytest for setup → oracle → grade (in Docker) | Claude |
| `harness tasks --benchmark X` (list tasks) | Claude |

**Done when:** tests show oracle solutions pass and empty workspaces fail, for every task, in Docker, via the adapter interface only.
**Explain-back:** Where do the two benchmarks' differences live, and why nowhere else? What would adding a third benchmark touch? What happens to a container if the process is killed mid-trial? Why is python-tasks setup baked into the image by `Benchmark.setup()` instead of run on the host?

## Phase 2 — (merged into Phase 1)

## Phase 3 — Agent adapter + trial runner (end of Part 1 core)

Decided 2026-10-29; details and alternatives in `agents-and-runner.md`. Ownership changed: Claude drafts everything, you review line by line.

| Piece | Owner |
|---|---|
| Agent registry `AGENTS = {"mini-swe-agent": MiniSweAgent}`; `--agent` = name or path (basename used); `--model` overrides `MSWEA_MODEL_NAME` from `.env` | Claude |
| `MiniSweAgent` adapter: mini's `DefaultAgent` + `get_model` on the host; env wrapper forwards `execute` to `env.exec`, raises `Submitted`, reports the container's OS; API key never enters the container | Claude, you review closely |
| Multi-turn: a second `send()` appends the user message to mini's history and continues | Claude |
| Reply = `Message("agent", (Text(submission),))`; mini's exit status (`Submitted` / `LimitsExceeded` / `TimeLimit`) kept in the trial's records | Claude |
| Limits: deadline checked in the command wrapper (raises `TimeLimit`), mini's cost/step limits, model-call timeout. Defaults: 10 min/trial, $1, 50 steps, 60 s/command; container `lifetime_s` = trial timeout + margin | Claude |
| Container death during the agent turn, decided from `docker inspect` (rule 6 in `agents-and-runner.md`); drop `--rm` from `docker run` (`stop()` still removes) | Claude |
| Trial runner: start container → `run_agent` (wrapped session) → container died? → kill leftovers → `grade` (`GraderError` → `grader_error`) → write outcome/transcript/trace → stop container (always) | Claude, you review closely |
| Per-attempt files until the DB: `runs/<run_id>/<trial_id>/attempt-<n>/` with `outcome.json`, `transcript.json`, `trajectory.json`; record agent name, model, agent submodule SHA per run | Claude |
| `harness run` synchronous — runs trials in-process, prints results | Claude |

**Done when:** `harness run --agent agents/mini-swe-agent --benchmark bash-operations --task_ids bash-001` passes in Docker.

- **DECIDED (2026-10-29):** container `lifetime_s` is linked to the trial limit (timeout + margin), no longer a fixed 7200 s.
- **DECIDED (2026-10-29):** `--agent agents/mini-swe-agent` resolves via the `AGENTS` registry by basename; model from `--model` or `.env`; agent name, model and submodule SHA recorded per run.
**Explain-back:** Walk through one trial line by line. What happens on agent timeout? Where does the trajectory end up? Why does mini run on the host? When is a dead container the agent's fault?

## Phase 4+5 — Database + queue + workers + status/results (merged 2026-10-29)

Merged because `harness run` now always goes through the queue (decisions 9–18 in `database.md`).
Ownership: Claude drafts everything; you review line by line (claim query, leases, retry policy closely).

| Piece | Owner |
|---|---|
| `store.py`: plain `sqlite3`, handwritten SQL; `runs/harness.db`, WAL, `busy_timeout`; schema per `database.md` + `schema_version` (mismatch → refuse, delete the file) | Claude |
| `harness run` inserts the run + trials and returns the run id; `--wait` runs an inline worker and prints results | Claude |
| Atomic claim (`BEGIN IMMEDIATE` + `UPDATE ... WHERE id=(SELECT ... LIMIT 1) RETURNING *`), 120 s lease renewed every 30 s, expired lease → stale attempt `infra_error` ("worker lost"), trial re-queued | Claude, you review closely |
| Queue-mode setup: first worker claims `runs.setup_claimed_by`, runs `setup()`, sets run `running`; setup failure → run `failed`, trials `infra_error` | Claude |
| `harness worker --concurrency N [--exit-when-idle]`: N threads, one trial (one container) each | Claude, you review closely |
| Retry policy: `infra_error` retried up to 3 attempts, short backoff; others final | Claude, you review closely |
| Events appended to `events.jsonl` as they happen, loaded into `events` after each turn; secrets redacted (`redact.py`) | Claude |
| `harness status` (active + last hour, `--all`; `--run_id` per trial) / `harness results --format summary|table|json` | Claude |
| Score → number: Inspect-style default, `Benchmark.score_to_float(score)` override (built in Phase 3) | You |

**Done when:** a completed run can be inspected entirely from the DB + files after the process exits; submit 3 runs back to back, start 2 workers, all finish; `kill -9` a worker mid-trial and its trial is reclaimed and completes.

- **DECIDED (2026-10-29):** schema and its decisions in `database.md`. Accuracy = mean `score_float` over `graded` trials; `grader_error`, `infra_error`, `unsupported` each reported separately (always shown, even 0).
- **DECIDED (2026-10-29), was OPEN:** bare `harness status` lists active runs + runs finished in the last hour (`--all` for every run); `harness status --run_id X` shows one run's trials.
- **DECIDED (2026-10-29):** `grader_error` is excluded from accuracy and reported prominently (count + list), because a grader crash is more likely a grader/harness problem than the agent's fault; agents destroying the world is rare, and `harness validate` keeps grader errors rare.
- **DECIDED (2026-10-29):** `error` is renamed `infra_error`; statuses are `graded`, `grader_error`, `infra_error`, `unsupported`.
- **DECIDED (2026-10-29):** who calls `Benchmark.setup()` in queue mode: the first worker to claim `runs.setup_claimed_by`; trials are claimable only after the run is `running`. The queue lives in `trials`, no jobs table.
- **DECIDED (2026-10-29):** a failed grader-dependency install (e.g. `apk add` with no network) is infrastructure, not the grader: `GraderSetupError` → `infra_error`, retried (`errors.md`).
- **DECIDED (2026-10-29):** threads, not processes, for `--concurrency` (I/O-bound; `database.md` #15).
**Explain-back:** Why an attempts table? What's in the DB vs on disk, and why? How is accuracy computed when some trials errored? How do two workers never grab the same trial? What bounds concurrency? What happens to a trial whose worker died? Why threads?

## Architecture audit (2026-10-29)

Decisions A–H built; findings, decisions and commits: `audit-2026-10-29.md`.

- **DEFERRED:** `Environment.exec(env=, user=)` parameters: add them with the second agent (Claude Code needs
  `forward_env` injected with `docker exec -e`, and the unprivileged `agent` user from `security.md` needs
  `docker exec -u`). Not needed while mini runs on the host as root.

## Phase 6 — Hardening

- Image pre-pull per run, per-trial cost/token tracking, LLM rate-limit backoff, `harness cancel`.
- Break things on purpose: bad image name, grader crash, agent infinite loop, Docker daemon restart.

**Done when:** every failure above ends in a clear status, no orphaned containers.

## Phase 7 — Extensions (pick by time left)

- Claude Code agent adapter (installed in container, `claude -p --output-format stream-json`). See also `adapters.md`.
- `harness validate --benchmark X` with `NullAgent` / `OracleAgent` (reuses Phase 1 oracle solutions).
- "Add a benchmark" Claude Code skill gated on `harness validate`.
- A GUI for the CLI (idea, 2026-10-29): e.g. `harness status --gui` serving a local live dashboard of runs/trials from `runs/harness.db`, or a terminal UI (live-updating `status`).

## Phase 8 — Deliverables

- README: setup, run instructions, write-up, design decisions (link `ideation/`).
- Architecture diagram (Excalidraw).
- A rehearsed 5-minute demo: submit runs → workers → status → results → kill a worker.
