# Decision: the harness owns containers; grading runs in the trial container

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

1. **Kill leftover agent processes** (as root): `kill -9 -1` when `agent_user`
   is root (kills everything except PID 1, the `sleep`); `pkill -9 -u agent`
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
