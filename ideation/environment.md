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
| Benchmark (`build_world`, `grade`) | `write_file`, `read_file`, `upload`, `download`, `exec` |
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

## Next

- `DockerEnvironment`: one container per trial; tests use it directly (needs Docker running).

Dropped: `LocalEnvironment` (2026-10-29) — Docker starts in ~1s, and a fake local world behaves differently from real containers (e.g. has python where alpine doesn't, needs `/workspace` path mapping) and is extra code to explain.
