# Agents and the trial runner (Phase 3)

**Status:** Decided 2026-10-29; built 2026-10-29 (`agents/`, `runner.py`,
`harness run`). Ownership: Claude drafts everything; Cindy reviews line by line.

## Decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| 1 | Agent registry like `BENCHMARKS`: `AGENTS = {"mini-swe-agent": MiniSweAgent}`. CLI `--agent` accepts a name or a path and uses its basename. `--model` overrides the default from `.env` (`MSWEA_MODEL_NAME`). Each run records agent name, model, and the agent submodule SHA | Same pattern as benchmarks; `--agent agents/mini-swe-agent` (the brief's form) still works; SHA + model make runs reproducible | Resolving a path to code/config at runtime (more magic, nothing gained with one agent) |
| 2 | mini-swe-agent runs on the **host** via its Python API (`DefaultAgent` from `minisweagent.agents.default` + `get_model` / `LitellmModel`). An env wrapper forwards its `execute({"command"})` to our `env.exec`, raises `Submitted` when output starts with `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, and reports the container's OS in template vars | The API key never enters the container; no install step inside the image; we see every command | Installing mini inside the container (needs python + the key in the world the agent controls); driving the `mini` CLI (setup wizard, less control) |
| 3 | Multi-turn continuation built **now**: a second `send()` appends the new user message to mini's history and continues the run | Cindy wants to test it on another benchmark; the turn model already allows several `send()`s | Deferring it (one turn per trial) |
| 4 | Reply = `Message("agent", (Text(submission),))`: only the agent's own output, nothing added by the harness. mini's exit status (`Submitted` / `LimitsExceeded` / `TimeLimit`) is kept in the trial's records | The harness adds nothing to what the grader sees; the exit status is still there for debugging | — |
| 5 | Limits: the trial deadline is checked in our command wrapper (the next command after the deadline raises `TimeLimit`), plus mini's own cost/step limits and a model-call timeout. Defaults: 10 min per trial, $1 cost, 50 steps, 60 s per command. Container `lifetime_s` = trial timeout + margin | All limits enforced in-process with no extra machinery; the container's self-destruct timer follows the trial | Running the agent in a separate process and killing it at the deadline (more machinery) |
| 6 | Container death during the agent's turn is decided by `docker inspect` evidence (table below) | Infra failures are retried and excluded; failures the agent caused must count against it | Treating every container death as `infra_error` (an agent could game failures into excluded trials) |
| 7 | Until the DB (Phase 4), each attempt writes `runs/<run_id>/<trial_id>/attempt-<n>/` with `outcome.json`, `transcript.json`, and mini's `trajectory.json` | Everything inspectable on disk now; Phase 4 indexes the same files | — |

### Rule 6: container died during the agent's turn

| Evidence from `docker inspect` | Outcome |
|---|---|
| Docker unreachable, or container vanished | `infra_error` |
| `OOMKilled: true` | `infra_error` for now (no per-container memory limit yet). Revisit when limits exist: then exceeding our limit is the agent's failure |
| Exited for any other reason (a process inside killed the main process) | `graded`, failed ("container died during agent turn") |
| Container died before the agent's turn | `infra_error` |
| Pre-grade kill or grading hits `DockerError` | re-check `docker inspect`: exited → `graded`, failed (the agent killed PID 1 just before handing back); otherwise `infra_error` |

Consequence: drop `--rm` from `docker run` so a dead container can still be
inspected. `stop()` still removes it. Expired containers are found by label
for a future `harness cleanup`.

## Trial runner flow

1. Start the container (`lifetime_s` = trial timeout + margin).
2. `run_agent` with the wrapped session (logs turns, enforces the deadline, builds the transcript).
3. Container died? Apply rule 6.
4. Kill leftover agent processes.
5. `grade` (`GraderError` → `grader_error`).
6. Write `outcome.json`, `transcript.json`, trajectory.
7. Stop the container (always, in `finally`).

## Built (2026-10-29): details and deviations to review

- **Pre-grade kill is not `kill -9 -1`.** Under `--init`, `kill -9 -1` also kills the main `sleep`, the init exits with it, and the container dies before grading. `kill_agent_processes()` kills everything except PID 1 and the sleep (`environment.md` #9).
- **mini config:** `mini.yaml` (what the `mini` CLI uses: tool calls, `LitellmModel`). Its `environment.env` (PAGER=cat etc.) isn't applied: our exec has no TTY, so pagers don't page.
- **Continuation:** mini ends a run with an `exit` message and, after a submit, an unanswered tool call. `MiniAgent.resume()` drops the `exit` marker, answers the pending tool call with the command's real output, appends the user message, and runs mini's loop again.
- **`Limits.max_turns` = 20** (new): the recording session raises `TurnLimit` past it; the runner then grades what the agent did.
- **Model-call timeout:** 120 s per call (`MODEL_TIMEOUT_S`); mini retries failed calls itself.
- **`Unsupported`:** mini raises it for any non-`Text` content.
- **`AgentLimitation`** (`agents/base.py`): the agent itself can't go on. mini raises it for litellm `ContextWindowExceededError` / `ContentPolicyViolationError`; the runner ends the turns and grades the world as it is (like a timeout), reason in `agent_exit_status`.
- **Any other exception** from the agent or harness (e.g. a model API auth error, provider down, rate limit after mini's retries) → `infra_error` with the traceback. All error paths: `errors.md`.
- **Container `lifetime_s`** = trial timeout + 600 s grading margin.
- **Score checks:** a non-JSON `Score.value`, or one `score_to_float` can't convert → `grader_error`.
