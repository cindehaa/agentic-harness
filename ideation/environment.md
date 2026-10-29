# The Environment interface

**Status:** Locked 2026-10-29. Code: `src/agentic_harness/environments/base.py`.

## Purpose

An `Environment` is one isolated world a trial runs in (e.g. one Docker
container): somewhere to run commands and move files. The harness owns it;
benchmarks and agents only get a handle.

## Who calls what

| Caller | Methods |
|---|---|
| Trial runner | `start` / `stop` (via `with env:`) |
| Benchmark (`run_agent`, `grade`) | `write_file`, `read_file`, `upload`, `download`, `exec` |
| Agent adapter | `exec` |

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

## Prior art

- **Harbor `BaseEnvironment`:** `start` / `stop` / `exec` / `upload` / `download`. Closest match; we add `write_file` / `read_file` and the str-or-list `exec`.
- **mini-swe-agent:** `execute` only. Enough for an agent, not for a benchmark that must stage files and grade.

## DockerEnvironment

Code: `src/agentic_harness/environments/docker.py`; tests: `tests/test_docker_environment.py` (need Docker running).

| # | Decision | Why |
|---|---|---|
| 1 | Drive the `docker` CLI via `subprocess` | No docker SDK dependency; same approach as mini-swe-agent |
| 2 | No bind mount; the workspace lives only in the container | Callers use `download()` to get it out |
| 3 | Main process is `sleep <lifetime_s>` (default 7200) | Keeps the container alive; self-destruct timer if the harness dies |
| 4 | Every container gets `--label agentic-harness=1` and name `harness-<8 hex>` | Leftovers are easy to find and remove |
| 5 | `/workspace` is the working directory (`-w`, created at start) and `exec`'s default cwd | One predictable place for task files |
| 6 | No CPU/memory limits yet | Not needed until we run many trials at once |
| 7 | `DockerError` (→ `infra_error`) only when the container is verifiably gone (`_alive()` check); anything the agent can cause (bad cwd, OCI exec error, a printed fake docker message) stays a normal command failure | Prevents gaming failures into excluded trials |

**Caveat:** on an `exec` timeout we kill the `docker exec` client, which does NOT kill
the process inside the container. Leftover processes are killed before grading
(`kill -9 -1`); Phase 6 may harden this.

**Known gaps (Phase 6):**
- A timed-out `exec` keeps running inside the container until the pre-grade `kill -9 -1`.
- The first `docker pull` can hang silently in sandboxed shells → pre-pull images once per run.
- `download()` copies symlinks as symlinks, so a link the agent planted can point at a host path when read later.
- DockerError (→ infra_error) only when the container is verifiably gone; anything the agent can cause stays a normal command failure (prevents gaming failures into excluded trials). Remaining hole: if the agent itself kills the container, that still reads as infra (planned fix in Phase 3, below).

**Planned for Phase 3** (decided 2026-10-29, `agents-and-runner.md`):
- Drop `--rm` from `docker run` so a container that dies during the agent's turn can be `docker inspect`ed: vanished / Docker unreachable / `OOMKilled` → `infra_error`; exited otherwise → `graded`, failed. `stop()` still removes the container; expired containers are found by label for a future `harness cleanup`.
- `lifetime_s` = trial timeout + margin instead of the fixed 7200 s default.

Dropped: `LocalEnvironment` (2026-10-29) — Docker starts in ~1s, and a fake local world behaves differently from real containers (e.g. has python where alpine doesn't, needs `/workspace` path mapping) and is extra code to explain.
