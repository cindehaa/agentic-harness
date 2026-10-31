# Implementation plan

**Status (2026-10-30):** phases 0–8 are built, plus agents over ACP (Claude Code among them), MCP sidecars, cost + time
tracking, the dashboard, and the adapter checks (`validate`, `parity`, `adapt`). What is left is listed under
"Remaining: extensions (not built)" at the end.

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
| Workspace lives in the container (no bind mount; `download()` gets it out). Grading always runs in the trial container, after the agent (**superseded 2026-10-29**: a named volume per trial, grading in an attached grader sidecar, `environment-and-grading.md` #7) | You |
| Guaranteed cleanup (`finally`, container labels, `harness cleanup`) | Claude |
| `BashOperations`, `PythonTasks` adapters (world setup, incl. python-tasks' `prepare.py`, baked into images in `Benchmark.setup()`) | You (first), Claude (second, for comparison) |
| Hand-written oracle solutions + pytest for setup → oracle → grade (in Docker) (moved into the adapters as `reference_solution`, 2026-10-29, for `harness validate`) | Claude |
| `harness tasks --benchmark X` (list tasks) (**not built**) | Claude |

**Done when:** tests show oracle solutions pass and empty workspaces fail, for every task, in Docker, via the adapter interface only.
**Explain-back:** Where do the two benchmarks' differences live, and why nowhere else? What would adding a third benchmark touch? What happens to a container if the process is killed mid-trial? Why is python-tasks setup baked into the image by `Benchmark.setup()` instead of run on the host?

## Phase 2 — (merged into Phase 1)

## Phase 3 — Agent adapter + trial runner (end of Part 1 core)

Decided 2026-10-29; details and alternatives in `agents-and-runner.md`. Ownership changed: Claude drafts everything, you review line by line.

| Piece | Owner |
|---|---|
| Agent registry `AGENTS = {"mini-swe-agent": MiniSweAgent}`; `--agent` = name or path (basename used); `--model` overrides `MSWEA_MODEL_NAME` from `.env` (**superseded 2026-10-30**: `HARNESS_MODEL`) | Claude |
| `MiniSweAgent` adapter: mini's `DefaultAgent` + `get_model` on the host; env wrapper forwards `execute` to `env.exec`, raises `Submitted`, reports the container's OS; API key never enters the container | Claude, you review closely |
| Multi-turn: a second `send()` appends the user message to mini's history and continues | Claude |
| Reply = `Message("agent", (Text(submission),))`; mini's exit status (`Submitted` / `LimitsExceeded` / `TimeLimit`) kept in the trial's records | Claude |
| Limits: deadline checked in the command wrapper (raises `TimeLimit`), mini's cost/step limits, model-call timeout. Defaults: 10 min/trial, $1, 50 steps, 60 s/command; container `lifetime_s` = trial timeout + margin (audit: 3 × timeout + 600 s) | Claude |
| Container death during the agent turn, decided from `docker inspect` (rule 6 in `agents-and-runner.md`); drop `--rm` from `docker run` (`stop()` still removes) | Claude |
| Trial runner: start container → `run_agent` (wrapped session) → container died? → kill leftovers → `grade` (`GraderError` → `grader_error`) → write outcome/transcript/trace → stop container (always) | Claude, you review closely |
| Per-attempt files until the DB: `runs/<run_id>/<trial_id>/attempt-<n>/` (now `attempt-<attempt_id>`, `database.md` #19) with `outcome.json`, `transcript.json`, `trajectory.json`; record agent name, model, agent submodule SHA per run | Claude |
| `harness run` synchronous — runs trials in-process, prints results | Claude |

**Done when:** `harness run --agent agents/mini-swe-agent --benchmark bash-operations --task_ids bash-001` passes in Docker.

- **DECIDED (2026-10-29):** container `lifetime_s` is linked to the trial limit (timeout + margin), no longer a fixed 7200 s. (Audit: 3 × timeout + 600 s.)
- **DECIDED (2026-10-29):** `--agent agents/mini-swe-agent` resolves via the `AGENTS` registry by basename; model from `--model` or `.env` (2026-10-30: `HARNESS_MODEL`); agent name, model and submodule SHA recorded per run.
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
- **DECIDED (2026-10-29):** a failed grader-dependency install (e.g. `apk add` with no network) is infrastructure, not the grader: `GraderSetupError` → `infra_error`, retried (`errors.md`). **Superseded 2026-10-29:** grading runs in an attached grader sidecar with its own image (`environment-and-grading.md` #7); nothing is installed, `GraderSetupError` is gone.
- **DECIDED (2026-10-29):** threads, not processes, for `--concurrency` (I/O-bound; `database.md` #15).
**Explain-back:** Why an attempts table? What's in the DB vs on disk, and why? How is accuracy computed when some trials errored? How do two workers never grab the same trial? What bounds concurrency? What happens to a trial whose worker died? Why threads?

## Architecture audit (2026-10-29)

Decisions A–H built; findings, decisions and commits: `audit-2026-10-29.md`.

- **DEFERRED:** `Environment.exec(env=, user=)` parameters: add them with the second agent (Claude Code needs
  `forward_env` injected with `docker exec -e`, and the unprivileged `agent` user from `security.md` needs
  `docker exec -u`). Not needed while mini runs on the host as root. **Built 2026-10-29** with Claude Code
  (`exec(env=, user=)`; values passed as `docker exec -e NAME`, so never on the host's command line).

## Grader sidecar (2026-10-29)

- **DECIDED + built:** general grading via an attached grader sidecar (`environment-and-grading.md` #7):
  workspace = named volume per trial; `Environment.attach_grader(image)`; `Benchmark.grader_image(task)`;
  `grade(task, grader, env, transcript)`; grader files only ever in the grader.

## Action space, MCP and Claude Code (2026-10-29)

Decisions and survey: `action-space.md`.

- **DECIDED + built:** agents act through their own native tools plus benchmark-specific actions from MCP servers
  the task declares (`Benchmark.mcp_servers(task)`, run as network-only attached sidecars; `Environment.attach(Sidecar)`,
  `attach_grader` kept as the grader case). One capability check in the runner (`AgentCapabilities` vs
  `Benchmark.requirements(task)` → `unsupported`). mini stays bash-only (`mcp=False`).
- **DECIDED + built:** `ClaudeCode` adapter (`agents/claude_code.py`, `"claude-code"`): runs inside the world from a
  cached derived image (`Agent.prepare_image`), key forwarded per command, `--mcp-config` from the task's servers,
  `--resume` for later turns, stream-json → `trajectory.jsonl` + `tool_call` / `tool_result` events
  (`AgentSession.new_events`). Live: bash-operations 0.80, python-tasks 1.00, MCP fixture pass (Haiku 5.5, $0.012 total).
  **Replaced 2026-10-30** by generic ACP support (`AcpAgent`, `agents/acp.py`; `claude-code` is a definition in
  `agents/acp_agents.toml`; `action-space.md` → Agents over ACP). Live through ACP: the same scores.

## Scale-up design (talking points, not built)

- **Pipelined grading.** After the agent hands back, the trial moves to a `grading` state that a separate
  grading pool picks up, so agent slots free up while graders run. The trial container must stay alive
  until it is graded (live-state grading needs it), so containers outlive the agent slot; the queue needs a
  second claimable state and its own lease. Small gain while graders are fast (bash-operations: ~1 s);
  worth it for slow graders (long test suites, browser checks).

## Phase 6 — Hardening

**Done 2026-10-29:**
- `harness results` defaults to `--format summary`; wall time = first trial start → last trial end (`attempts.started_at` / `finished_at`, schema v3), queue wait shown separately.
- Graceful Ctrl-C / SIGTERM for `harness worker` and `run --wait`: stop claiming, stop the worlds in flight (sidecars, volumes), close their attempts as retryable `infra_error` ("worker interrupted", re-queued at once), release setups, delete the worker row, exit; a second signal exits at once (`errors.md`).
- `harness cleanup` (`--all`, `--images`): labelled containers and volumes of no live attempt; cached `harness-*` images no unfinished trial needs (`environment.md` DockerEnvironment #4).
- Image pre-pull at run setup: base, grader and MCP-server images pulled explicitly, time-bounded; agent images built (`environment.md` #11).
- `kill_agent_processes` repeats its `/proc` pass (fork loops); workspace volume removal retried, leftovers logged.
- `harness status` warns when trials wait and no worker is running.
- Break-it cases covered by tests: bad image name, grader crash, fork loop, container killed from inside, worker killed (lease) or interrupted (signal).
- 2026-10-30: `harness worker --max-running N` (global cap across workers, checked in the claim; `database.md` #15);
  mini's model calls capped at the trial's remaining time (`agents-and-runner.md` → Built); `run --concurrency`
  without `--wait` is a usage error.
- Cost + time tracking (schema v4, `database.md` #20–23): tokens / model calls / cost per attempt, always recorded (error, timeout, interrupt); phase timestamps; `harness run --max-run-cost` (`skipped_budget`); `status` live spend + elapsed; `results` cost per (passing) task, tokens, time breakdown, throughput.

**Not done:** `harness cancel`, a Docker daemon restart mid-run (untested; containers then read as gone → retryable `infra_error`).

## Phase 7 — Extensions (pick by time left)

Built:

- ~~Claude Code agent adapter~~ built 2026-10-29 (`action-space.md`).
- ~~`harness validate --benchmark X` with `NullAgent` / `OracleAgent`~~ built 2026-10-29 (reference solutions moved
  into the adapters; `adapters.md` → Built).
- ~~"Add a benchmark" Claude Code skill gated on `harness validate`~~ built 2026-10-29
  (`.claude/skills/add-benchmark/SKILL.md` + `harness adapt`); proof: the `terminal-bench` adapter (3 tasks),
  drafted by `harness adapt`, hand-fixed, 2026-10-30.
- ~~`harness parity`~~ built 2026-10-29 (baseline C: `Benchmark.original_workflow`).
- ~~A GUI for the CLI (idea: `harness status --gui` or a TUI)~~ built 2026-10-29 as `harness dashboard` (read-only
  local web view).
- One harness-wide model setting (`HARNESS_MODEL`, `Agent.native_model()`), 2026-10-30.

Not built: τ²-bench (designed, out of scope for the take-home): an adapter whose `mcp_servers(task)` serves
τ²'s own code from one MCP sidecar: the domain's tools (airline / retail / telecom) and the simulated user
(as a tool, or driven by `run_agent` as a second agent), graded by τ²'s own evaluator (reward from the final
domain database state + the conversation). Precedent: Harbor's tau3 adapter (MCP sidecar). Needs `grade()` to
reach the service sidecar's state (open question in `action-space.md`); mini is `unsupported` for it.

## Phase 8 — Deliverables

Built 2026-10-29, kept current since:

- README: setup, run instructions, write-up, design decisions (link `ideation/`).
- Architecture diagram: Mermaid in the README, `docs/architecture.excalidraw`, `docs/architecture.md`.

## Remaining: extensions (not built)

Each is designed or noted in the linked doc; none is needed for Parts 1–2.

- `harness cancel` (Phase 6 leftover) and `harness tasks` (Phase 1).
- `harness regrade` / grading from a snapshot (`trial-phases.md` → If we revisit).
- Pipelined grading pool (Scale-up above).
- Multi-machine workers: SQLite → Postgres with SQLAlchemy Core + Alembic (`database.md` #8, #10).
- The unprivileged `agent` user and `agent_user` (`security.md` (a)); network as a benchmark-declared world
  property with a model-API-only allowlist (`environment.md` → Network, `security.md` → Network).
- τ²-bench via an MCP sidecar (Phase 7); GUI / desktop benchmarks (`action-space.md` → Still open).
- Parity beyond mini-swe-agent on the two starter benchmarks: `original_workflow` for Claude Code and for
  Terminal-Bench (`adapters.md` → Built).
- Open for Cindy: our `MiniSweAgent` runs commands with `sh -c`, mini's own Docker mode with `bash -lc`, a
  difference parity exposed (`adapters.md` → Built; its `environment.env` difference was fixed 2026-10-30).
