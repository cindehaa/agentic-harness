# Decision: the harness owns containers; grading runs in an attached grader sidecar

## 1. The harness owns every container

**Decision:** the harness alone starts, `exec`s into, and stops containers.
Agents never start containers. They receive an `Environment` handle
(`exec`, copy in/out) and send their commands through it. Only the harness
writes to the DB. Neither the agent nor anything inside the container does.

- Each trial container starts with `sleep <trial timeout + margin>` as its
  main process. A container lives only while its main process runs, so this
  keeps it alive for `docker exec`. It also acts as a self-destruct timer:
  if the harness crashes without cleaning up, the container still exits
  shortly after the trial would have timed out.
- This keeps us from overfitting to mini-swe-agent. Its `DockerEnvironment`
  (in the submodule) starts its own container. Our `MiniSweAgent` adapter
  instead hands it an object that forwards commands to our `env.exec()`, and
  Claude Code gets the same handle.

**Why:** one place controls the container lifecycle, timeouts, and cleanup.
Results can't be forged from inside the sandbox: the harness runs the
grader, reads its output, and writes the row.

## 2. Grading runs in the same trial container, after the agent

> **Superseded 2026-10-29 by #7 (attached grader sidecar).** Kept for the reasoning.

**Decision:** start env → agent → grade all run in one trial container. There is
no per-adapter choice of where grading runs.

### Options considered

All three are one trial job. The question was only how many containers that
job uses.

| | 1. Same container ✅ | 2. Grade on host | 3. Fresh grader container |
|---|---|---|---|
| Works on images without the grader's tools (alpine has no python) | ✅ with grade-time install (below) | ✅ | ✅ |
| Agent can tamper with grader | mitigated by copying grader files in late | no | no |
| Runs agent-written code on the host | no | **yes** (python-tasks' pytest imports `solution.py`) | no |
| Can check live state ("is nginx running?") | **yes** | no | no |
| Same rule for every benchmark | yes | yes | only for file-based grading |
| Extra cost | none | none | ~1 s container start |

### Prior art

| Harness | Where grading runs |
|---|---|
| Harbor / Terminal-Bench | Same container; tests are copied in after the agent finishes |
| METR (Vivaria) | Same environment (`TaskFamily.score()`) |
| Inspect AI | Same sandbox (solver, then scorer) |
| SWE-bench | Fresh container. The exception: the agent's output is a small patch, so it travels easily |

### Why same container won

- It is the most general option. It is the only one that supports benchmarks
  that check live state, so it fits "run any agentic benchmark".
- Adapters don't choose where grading runs, so adding a benchmark has one
  less decision.
- It matches what most agentic harnesses do.

A separate grader container stays available as future hardening and as an
onsite talking point.

## 3. Pre-grade step (in order)

> **Updated 2026-10-29 (#7):** steps 2–3 below are gone. Grader files go only into the attached
> grader, and its image already has the grader's tools. Today: kill leftovers → save
> `workspace.tar` → start the grader from `grader_image(task)` → `grade(task, grader, env, transcript)`
> → stop the grader, then the world.

1. **Kill leftover agent processes** (as root): `Environment.kill_agent_processes()`
   when `agent_user` is root: every process except PID 1 (the init) and its main
   `sleep` (not a plain `kill -9 -1`: that also kills the sleep, and the container
   dies before grading; `environment.md` #9); `pkill -9 -u agent`
   when the agent runs as the unprivileged `agent` user (see `security.md`).
   Either way, nothing the agent left running can interfere with grading.
2. **Copy the benchmark's grader files in late.** They enter the container
   only now, so the agent never saw them and couldn't edit them.
3. **Install grader dependencies**, e.g. `apk add python3` on alpine.
4. **Run the grader.**

Steps 2–3 are implemented by each adapter's `grade()`. The harness provides
the ordering and the late-copy mechanism.

We do **not** restore files the benchmark puts in the agent's workspace (e.g.
python-tasks' `test_solution.py`, which its `grade.py` runs as-is): that would
change the benchmark. See `fidelity-vs-integrity.md`.

## 4. Outcome rules

Full model: `task-input.md` → "Outcome model". Statuses are exactly:

| What happened | Status | Notes |
|---|---|---|
| Container died, image pull failed (harness/infra failure) | `infra_error` | Excluded from accuracy; retried per retry policy. Renamed from `error` (DECIDED 2026-10-29). |
| Agent ran, then the grader crashed or its output was unparseable | `grader_error` | Excluded from accuracy; reported prominently in results (count + list), grader stderr saved as an artifact. **DECIDED (2026-10-29):** excluded, not 0, because a grader crash is more likely a grader/harness problem than the agent's fault; agents destroying the world is rare, and `harness validate` keeps grader errors rare. |
| Agent can't handle the task's content | `unsupported` | Excluded from accuracy; reported separately |
| Grader ran and returned a `Score` | `graded` | Normal. The score is converted to a number for accuracy (Inspect-style default, overridable per benchmark). Accuracy = mean over `graded` trials only |

This supersedes the earlier `passed` / `failed` (+ reason `grader_error`) /
`error` model.

## 5. Logs never live only in the container

Every `exec`'s output streams through the harness and is written to the
attempt's artifact directory on the host
(`runs/<run_id>/<trial_id>/attempt-<n>/`) as it happens. If the container
dies mid-trial, everything up to that point is already saved.

## 6. Out of scope

- **Hash-verifying protected files** (checking that tests and grader weren't
  modified). Built as `protected_files` and removed 2026-10-29 as overfitting.
  Late copying protects the grader; integrity evidence otherwise comes from
  the trace and the saved final workspace (`fidelity-vs-integrity.md`).

## Decided: install grader dependencies at grade time

> **Superseded 2026-10-29 by #7.** Nothing is installed at grade time; the grader's image
> carries its tools, and grading needs no network.

Agent runtime and grade time stay separate: the agent sees exactly the
environment the benchmark specified, and grader dependencies are installed in
the pre-grade step (e.g. `apk add python3`). Costs a few seconds per trial and
needs network at grade time.

Rejected: baking them into a derived image once per run. Faster, but the agent
would see the extra tools (e.g. python on a bash-only benchmark), which changes
what the benchmark measures.

Prior art: Terminal-Bench 2 / Harbor tasks install test deps inside
`tests/test.sh` at verify time; METR installs everything at build time but
hides scoring assets from the agent with user permissions (agent runs as a
non-root user); SWE-bench bakes test deps into the instance image because they
are the repo's own deps.

## Known gaps

- ~~**Grading needs the network**~~ (fixed 2026-10-29 by #7: the grader image is
  pre-pulled in `setup()` with `ensure_image`; nothing is installed at grade time). Was:
  (bash-operations: `apk add python3` at grade
  time). With no network the install fails; since 2026-10-29 that is a
  `GraderSetupError` → `infra_error` (retried), not a `grader_error`, because
  the grader never ran. Phase 6 option B: install grader dependencies into a
  root-only prefix (e.g. `/grader-deps`, mode 700) when the image is built, so
  grading needs no network and the agent (once it runs as the unprivileged
  `agent` user, `security.md`) still can't see or use them.

## 7. General grading: an attached grader sidecar (DECIDED 2026-10-29)

Supersedes #2 (same container), the grade-time install, and "copy grader files in late".

**Problem.** Grading needs two things at once: the grader's own tools (python for
`evaluate.py`) and access to the evidence (files, live services, processes).
Same-container grading had the evidence but not the tools: it forced `apk add python3`,
i.e. the adapter picking dependencies for an OS, plus a network dependency at grade
time. Host grading has the tools but no live state, and runs agent-written code on the host.

**Decision.**

- The world's `/workspace` is a Docker named volume per trial container (created at
  start, labelled `agentic-harness=1`, removed in `stop()`).
- `Environment.attach_grader(image) -> Environment`: a second world that shares the
  trial's workspace (`--volumes-from`), network (`--network container:<trial>`, so
  `localhost` is the trial's) and processes (`--pid container:<trial>`), with the same
  resource limits. Stopped with the trial (the runner's `with`, and the trial's `stop()`
  removes its graders first). The base interface says what is shared, not how.
- `Benchmark.grader_image(task) -> str`, default `task.image` (the world's own tools).
  `grade(task, grader, env, transcript)`: `grader` is the sidecar; `env` (the trial
  container) stays available for checks only the world's own programs can do.
- Runner: agent turn → container-death check → kill leftovers → save `workspace.tar` →
  start the grader → `grade` → stop both. The grader failing to start is
  `EnvironmentLost` → `infra_error` (retryable; if the trial itself died, rule 6 decides
  instead). `GraderError` → `grader_error`. `GraderSetupError` removed: nothing is set up.
- bash-operations: `grader_image` = `python:3.11-slim`, pre-pulled in `setup()`
  (`ensure_image`); `grade` uploads the benchmark folder to `/grader` **in the grader**
  and runs `python /grader/evaluate.py --workspace-dir /workspace --task <id>` there.
  No `apk add`. python-tasks: no override (its task image has python + pytest from
  `prepare.py`); `grade.py` runs in the grader.
- **Grader files live only in the grader container**: the agent's world never gets a
  `/grader` folder (replaces "copy grader files in late"). Tested: after grading, the
  world still has no python and no `/grader`.

### Options

| | Same container | Bake grader deps into the image | Grade on host | Attached sidecar ✅ |
|---|---|---|---|---|
| Grader has its tools | only by installing them at grade time (`apk add`: per-OS, needs network) | yes | yes | yes (its own image) |
| Agent's world unchanged (no extra tools for the agent) | no (after grading only) | **no**: the agent sees python on a bash-only benchmark | yes | yes |
| Grader files hidden from the agent | copied in late | need root-only files + unprivileged agent | yes | yes: never in the world |
| Files | yes | yes | via `download()` | yes (shared volume) |
| Live services (`localhost:8080`) | yes | yes | no | yes (shared network namespace) |
| Processes (`ps`) | yes | yes | no | yes (shared PID namespace) |
| Network at grade time | needed (install) | no | no | no (grader image pre-pulled) |
| Agent-written code on the host | no | no | **yes** (pytest imports `solution.py`) | no |
| Cost | install time per trial | one derived image per task | none | ~1 s container start |

Prior art: Kubernetes sidecars / `kubectl debug --target` (share a pod's namespaces);
SWE-bench uses a fresh container but only because its evidence is a patch; Harbor,
METR and Inspect grade in the same environment.

**Deferred (talking point):** pipelining grading — `implementation-plan.md` → Scale-up.
