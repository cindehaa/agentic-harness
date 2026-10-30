# The Environment interface

**Status:** Locked 2026-10-29. Code: `src/agentic_harness/environments/base.py`.

## Purpose

An `Environment` is one isolated world a trial runs in (e.g. one Docker
container): somewhere to run commands and move files. The harness owns it;
benchmarks and agents only get a handle.

## Who calls what

| Caller | Methods |
|---|---|
| Trial runner | `start` / `stop` (via `with env:`), `attach_grader` (2026-10-29) |
| Benchmark (`run_agent`, `grade`) | `write_file`, `read_file`, `upload`, `download`, `exec`; `grade` gets the attached grader and the trial's world |
| Agent adapter | `exec`, through a `RecordingEnvironment` that traces every command into `events.jsonl` (2026-10-29 audit) |

## Decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| 1 | Context manager (`with env:` calls `start`, then `stop`) | The container is always stopped, even on error | Manual `start`/`stop` that a crash can skip |
| 2 | `exec` takes `str` (runs via `sh -c`) or `list[str]` (runs the program directly) | Agents send shell lines; graders/harness want no quoting surprises | Strings only (graders must quote) or lists only (agents must split) |
| 3 | Separate `stdout` / `stderr`, plus `timed_out` | Graders need the split; a timeout is not an exit code. An agent adapter merges them if its agent wants one stream | One merged `output` field (can't be un-merged) |
| 4 | Both `write_file` / `read_file` and `upload` / `download` | Small in-memory data vs files/folders on disk (grader scripts in, workspace out) | Only upload/download (temp files for every small write) |
| 5 | No `capabilities` field | Nothing to describe until a second capability (e.g. a browser) exists | A capabilities dict now, guessed ahead of need |
| 6 | `image` in the constructor | One object = one container; no reuse across trials | `start(image)`, which lets one object hold different worlds over time |
| 7 | Default `exec` timeout 60s, overridable per call; `None` = no timeout | Stops one hung command eating the whole trial budget | No default (a hang blocks until the trial limit) |

### Ports and adapters (2026-10-29 audit)

| # | Decision | Why |
|---|---|---|
| 8 | `state() -> EnvironmentState | None` (`running`, and once ended an `EndReason`: `EXPIRED` → `infra_error` retried, `RESOURCE_LIMIT` (over limits we set) → graded fail, `KILLED` (ended from inside) → graded fail, `UNKNOWN` → `infra_error`; None = gone) and `kill_agent_processes()` are on the ABC. 2026-10-29: `EndReason` replaced `exit_code` / `oom_killed`; each backend translates its own evidence | The runner's container-death rule and pre-grade kill work for any backend; no exit codes or OOM knowledge in `runner.py` |
| 9 | `EnvironmentLost` (in `base.py`) is the backend-neutral "the world is gone" error; `DockerError` subclasses it | The runner catches `EnvironmentLost` and never imports Docker |
| 10 | The runner takes an `EnvironmentFactory` = `(image, lifetime_s, limits) -> Environment`; the worker injects `DockerEnvironment` (its constructor is one) | The runner is testable with a fake environment (`tests/test_runner_rules.py`, no Docker) |
| 11 | `build_image(dockerfile, context, tag)` is exported by the `environments` package as the image-building port; benchmarks import it from there, never from `docker.py`. `ensure_image(image)` (2026-10-29) pulls an image only if it isn't local, bounded by a 600 s timeout (a first pull can hang silently): benchmarks' `setup()` pull their base and grader images explicitly before building, and run setup then makes every grader and MCP-server image local and builds the agent's derived images (`Agent.prepare_image`), so no trial waits on a first pull or build | Benchmarks name images, not a container backend |
| 12 | `save_workspace(local, max_bytes) -> truncated`: the working directory as a tar file, streamed out (Docker: `tar` via `docker exec`), capped (the runner uses 50 MB), never unpacked on the host | Integrity evidence (`fidelity-vs-integrity.md`) without `download()`'s symlink hazard |
| 13 | `attach_grader(image) -> Environment` (2026-10-29): a second, not-yet-started world from `image` that shares this one's workspace, network and processes, with the same limits; stopped with this world at the latest (`environment-and-grading.md` #7) | Grading needs the grader's own tools *and* the evidence (files, live services, processes); the port says what is shared, not how |

## Prior art

- **Harbor `BaseEnvironment`:** `start` / `stop` / `exec` / `upload` / `download`. Closest match; we add `write_file` / `read_file` and the str-or-list `exec`.
- **mini-swe-agent:** `execute` only. Enough for an agent, not for a benchmark that must stage files and grade.

## DockerEnvironment

Code: `src/agentic_harness/environments/docker.py`; tests: `tests/test_docker_environment.py` (need Docker running).

| # | Decision | Why |
|---|---|---|
| 1 | Drive the `docker` CLI via `subprocess` | No docker SDK dependency; same approach as mini-swe-agent |
| 2 | No bind mount; the workspace is a Docker named volume per trial container (2026-10-29; was: only in the container's own filesystem), created at `start()` with label `agentic-harness=1`, removed in `stop()` after its sidecars and container (retried 4 times with a short backoff; a volume that stays is logged as leftover and `harness cleanup` removes it). A new volume starts with the image's own `/workspace` files (Docker copies them in) | An attached grader can share it (`--volumes-from`); callers still use `download()` / `save_workspace()` to get it out. Side effect: `rm -rf /workspace` only empties it, the mount point stays |
| 3 | Main process is `sleep <lifetime_s>` (default 7200) | Keeps the container alive; self-destruct timer if the harness dies |
| 4 | Every container gets `--label agentic-harness=1` and name `harness-<8 hex>` | Leftovers are easy to find and remove |
| 5 | `/workspace` is the working directory (`-w`, created at start) and `exec`'s default cwd | One predictable place for task files |
| 6 | Per-container limits from `Limits` (2026-10-29 audit): `--memory` (2g), `--cpus` (1.0), `--pids-limit` (512) | Many trials run at once; one agent must not starve the others or fork-bomb the host. OOM-killed under our limit = the agent's failure (`graded`) |
| 7 | `DockerError` (→ `infra_error`) only when the container is verifiably gone (`_alive()` check); anything the agent can cause (bad cwd, OCI exec error, a printed fake docker message) stays a normal command failure | Prevents gaming failures into excluded trials |
| 8 | No `--rm` (2026-10-29): a container that dies mid-trial stays until `stop()` removes it, so `state()` (`docker inspect`: `Status`, `Running`, `ExitCode`, `OOMKilled`; None = gone or Docker unreachable) can tell the runner why it died. `_end_reason()` translates: `Status` not `exited` → `UNKNOWN`; `OOMKilled` → `RESOURCE_LIMIT`; exit code 0 (the `sleep` ran its course) → `EXPIRED`; other exit codes → `KILLED` | Rule 6 in `agents-and-runner.md` needs the evidence |
| 9 | `kill_agent_processes()` kills every process except PID 1 (the init) and the main `sleep` (its PID is read from `/proc/1/task/1/children` at start), via passes over `/proc` repeated until one kills nothing (at most 5; 2026-10-29: one pass missed 30-170 children of a fork loop, forked after `/proc` was listed) | A plain `kill -9 -1` also kills the sleep, and `--init` exits when its child does: the container dies before grading (found while building it) |
| 10 | `attach_grader(image)`: `docker run --volumes-from <trial> --network container:<trial> --pid container:<trial>` with the trial's `--memory` / `--cpus` / `--pids-limit` and lifetime, the same label; the trial's `stop()` stops its graders first (a volume in use can't be removed) | One Docker flag per thing shared; nothing is copied. Inside the grader, `localhost` and `ps` are the trial's |

**Caveat:** on an `exec` timeout we kill the `docker exec` client, which does NOT kill
the process inside the container. Leftover processes are killed before grading
(`kill_agent_processes()`, repeated passes over `/proc`, so a fork loop can't outrun it).

**Known gaps (Phase 6):**
- A timed-out `exec` keeps running inside the container until the pre-grade `kill_agent_processes()`.
- The first `docker pull` can hang silently in sandboxed shells → pre-pull images once per run.
- `download()` copies symlinks as symlinks, so a link the agent planted can point at a host path when read later. The runner saves the final workspace with `save_workspace()` (a tar, never unpacked) instead.
- DockerError (→ infra_error) only when the container is verifiably gone; anything the agent can cause stays a normal command failure (prevents gaming failures into excluded trials). Remaining hole: if the agent itself kills the container, that still reads as infra (planned fix in Phase 3, below).

**Phase 3** (decided 2026-10-29, `agents-and-runner.md`; `--rm` dropped and `state()` added, decisions 8-9 above):
- Drop `--rm` from `docker run` so a container that dies during the agent's turn can be `docker inspect`ed: vanished / Docker unreachable → `infra_error`; exited non-zero or `OOMKilled` → `graded`, failed; exited 0 (lifetime ran out) → `infra_error` (2026-10-29 audit). `stop()` still removes the container; expired containers are found by label for a future `harness cleanup`.
- `lifetime_s` = 3 × trial timeout + 600 s margin (2026-10-29 audit; was timeout + margin) instead of the fixed 7200 s default. A keep-alive that exits with code 0 ran out of lifetime → `infra_error`.

Dropped: `LocalEnvironment` (2026-10-29) — Docker starts in ~1s, and a fake local world behaves differently from real containers (e.g. has python where alpine doesn't, needs `/workspace` path mapping) and is extra code to explain.
