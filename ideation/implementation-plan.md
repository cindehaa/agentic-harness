# Implementation plan

Each phase ends with something runnable and a few "explain-back" questions —
if you can't answer them out loud, the phase isn't done. Ownership says who
writes the first draft: **You** = you write it, Claude reviews;
**Claude** = Claude drafts a small diff, you review/run/commit it.

Rule of thumb for ownership: you write the parts an interviewer will poke at
(interfaces, trial runner, claim query, error semantics); Claude drafts
plumbing (Docker SDK calls, CLI formatting, boilerplate SQL, tests).

---

## Phase 0 — Get oriented (no harness code)

- Commit the untouched starter code as a baseline so later diffs show only our work.
- `make install`, configure `mini-extra config setup`, run `make bash-example` and `make py-example` by hand.
- Read `benchmarks/*/setup.py|prepare.py|evaluate.py|grade.py` and mini-swe-agent's `environments/docker.py`.
- `docker run --rm -it alpine:latest sh` — confirm there's no python.

**Done when:** both examples pass locally.
**Explain-back:** How does each benchmark report pass/fail? Why can't `evaluate.py` run inside the alpine container? How does mini-swe-agent's DockerEnvironment execute a command?

## Phase 1 — Core types + benchmark adapters (no Docker yet)

| Piece | Owner |
|---|---|
| `Task`, `GradeResult`, `Benchmark` ABC, registry | You |
| `Environment` ABC + `LocalEnvironment` (temp dir, subprocess) | Claude |
| `BashOperations`, `PythonTasks` adapters | You (first), Claude (second, for comparison) |
| Hand-written oracle solutions + pytest for setup → oracle → grade | Claude |
| `harness tasks --benchmark X` (list tasks) | Claude |

**Done when:** tests show oracle solutions pass and empty workspaces fail, for every task, via the adapter interface only.
**Explain-back:** Where do the two benchmarks' differences live, and why nowhere else? What would adding a third benchmark touch?

## Phase 2 — Docker environment

| Piece | Owner |
|---|---|
| `DockerEnvironment`: start(image), exec(cmd, timeout), copy in/out, stop | Claude |
| Workspace bind mount; decide per adapter where grading runs (host vs container) | You |
| Guaranteed cleanup (`finally`, container labels, `harness cleanup`) | Claude |

**Done when:** Phase 1 tests pass against `DockerEnvironment` too.
**Explain-back:** What happens to a container if the process is killed mid-trial? Why does python-tasks setup run inside the container?

## Phase 3 — Agent adapter + trial runner (end of Part 1 core)

| Piece | Owner |
|---|---|
| `Agent` ABC (`run`, `parse_trajectory`) | You |
| `MiniSweAgent` adapter (drives the container from the host) | Claude, you review closely |
| Trial runner: phases, per-phase timeout, artifacts dir per attempt | You |
| `harness run` synchronous — runs trials in-process, prints results | Claude |

**Done when:** `harness run --agent agents/mini-swe-agent --benchmark bash-operations --task_ids bash-001` passes in Docker.
**Explain-back:** Walk through one trial line by line. What happens on agent timeout? Where does the trajectory end up?

## Phase 4 — Persistence + status/results

| Piece | Owner |
|---|---|
| SQLite schema: runs, trials, attempts, events, artifacts | You (design), Claude (DDL) |
| Recorder: phase events, grade results, artifact paths | Claude |
| Trajectory → normalized events for mini-swe-agent | Claude |
| `harness status` / `harness results --format json|table|summary` (accuracy, errors separated) | Claude |

**Done when:** a completed run can be inspected entirely from the DB + files after the process exits.
**Explain-back:** Why an attempts table? What's in the DB vs on disk, and why? How is accuracy computed when some trials errored?

## Phase 5 — Queue + workers (Part 2)

| Piece | Owner |
|---|---|
| `harness run` enqueues and returns a run_id immediately | Claude |
| Atomic claim query + leases/heartbeat | You |
| `harness worker --concurrency N` (process pool / threads) | Claude, you review closely |
| Retry policy: infra `error` vs agent `failed`, max attempts, backoff | You |

**Done when:** submit 3 runs back to back, start 2 workers, all finish; `kill -9` a worker mid-trial and its trial gets re-queued and completes.
**Explain-back:** How do two workers never grab the same trial? What bounds concurrency? What happens to a trial whose worker died?

## Phase 6 — Hardening

- Image pre-pull per run, per-trial cost/token tracking, LLM rate-limit backoff, `harness cancel`.
- Break things on purpose: bad image name, grader crash, agent infinite loop, Docker daemon restart.

**Done when:** every failure above ends in a clear status, no orphaned containers.

## Phase 7 — Extensions (pick by time left)

- Claude Code agent adapter (installed in container, `claude -p --output-format stream-json`). See also `adapters.md`.
- `harness validate --benchmark X` with `NullAgent` / `OracleAgent` (reuses Phase 1 oracle solutions).
- "Add a benchmark" Claude Code skill gated on `harness validate`.

## Phase 8 — Deliverables

- README: setup, run instructions, write-up, design decisions (link `ideation/`).
- Architecture diagram (Excalidraw).
- A rehearsed 5-minute demo: submit runs → workers → status → results → kill a worker.
