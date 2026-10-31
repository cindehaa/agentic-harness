# Agents and the trial runner (Phase 3)

**Status:** Decided 2026-10-29; built 2026-10-29 (`agents/`, `runner.py`,
`harness run`). Ownership: Claude drafts everything; Cindy reviews line by line. Later changes are marked in
place: the audit (Config, interface cleanups below), Claude Code + capabilities (`action-space.md`), cost tracking
(`database.md` #20–23), one harness-wide model (2026-10-30, Config).

## Decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| 1 | Agent registry like `BENCHMARKS`: `AGENTS = {"mini-swe-agent": "agentic_harness.agents.mini_swe_agent:MiniSweAgent"}`, imported only when used (lazy since the 2026-10-29 audit, so `status` / `results` / benchmarks never load litellm). CLI `--agent` accepts a name or a path and uses its basename. `--model` overrides the default from `.env` (`MSWEA_MODEL_NAME`; **superseded 2026-10-30** by `HARNESS_MODEL`, see Config). Each run records agent name, model, and the agent submodule SHA (since the audit: `Agent.version()`). `claude-code` added 2026-10-29 | Same pattern as benchmarks; `--agent agents/mini-swe-agent` (the brief's form) still works; SHA + model make runs reproducible | Resolving a path to code/config at runtime (more magic, nothing gained with one agent) |
| 2 | mini-swe-agent runs on the **host** via its Python API (`DefaultAgent` from `minisweagent.agents.default` + `get_model` / `LitellmModel`). An env wrapper forwards its `execute({"command"})` to our `env.exec`, raises `Submitted` when output starts with `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, and reports the container's OS in template vars | The API key never enters the container; no install step inside the image; we see every command | Installing mini inside the container (needs python + the key in the world the agent controls); driving the `mini` CLI (setup wizard, less control) |
| 3 | Multi-turn continuation built **now**: a second `send()` appends the new user message to mini's history and continues the run | Cindy wants to test it on another benchmark; the turn model already allows several `send()`s | Deferring it (one turn per trial) |
| 4 | Reply = `Message("agent", (Text(submission),))`: only the agent's own output, nothing added by the harness. mini's exit status (`Submitted` / `LimitsExceeded` / `TimeLimit`) is kept in the trial's records | The harness adds nothing to what the grader sees; the exit status is still there for debugging | — |
| 5 | Limits: the trial deadline is checked in our command wrapper (the next command after the deadline raises `TimeLimit`), plus mini's own cost/step limits and a model-call timeout. Defaults: 10 min per trial, $1 cost, 50 steps, 60 s per command. Container `lifetime_s` = trial timeout + margin (**superseded** by the audit: 3 × trial timeout + 600 s) | All limits enforced in-process with no extra machinery; the container's self-destruct timer follows the trial | Running the agent in a separate process and killing it at the deadline (more machinery) |
| 6 | Container death during the agent's turn is decided by `docker inspect` evidence (table below) | Infra failures are retried and excluded; failures the agent caused must count against it | Treating every container death as `infra_error` (an agent could game failures into excluded trials) |
| 7 | Until the DB (Phase 4), each attempt writes `runs/<run_id>/<trial_id>/attempt-<n>/` with `outcome.json`, `transcript.json`, and mini's `trajectory.json` (**superseded** by `database.md` #19f: `runs/<run_id>/<task_id>/attempt-<attempt_id>/`, plus `events.jsonl` and `workspace.tar`) | Everything inspectable on disk now; Phase 4 indexes the same files | — |

### Rule 6: container died during the agent's turn

Since 2026-10-29 the runner reads only `EnvironmentState.end_reason` (`environment.md` #8); `DockerEnvironment`
turns the `docker inspect` evidence below into `EndReason` (`RESOURCE_LIMIT`, `EXPIRED`, `KILLED`; a `Status` other
than `exited` → `UNKNOWN` → `infra_error`).

| Evidence from `docker inspect` | Outcome |
|---|---|
| Docker unreachable, or container vanished | `infra_error` |
| `OOMKilled: true` | `graded`, failed ("container exceeded its resource limits during agent turn"; was "ran out of memory"): every container has our `--memory` limit (`Limits.memory`, default 2g), so going over it is the agent's doing (2026-10-29 audit; was `infra_error` while there was no limit) |
| Exited with code 0: the keep-alive `sleep` ended on its own, i.e. its lifetime ran out (2026-10-29 audit) | `infra_error` (we sized it) |
| Exited with a non-zero code (a process inside killed the main process) | `graded`, failed ("container died during agent turn") |
| Container died before the agent's turn | `infra_error` |
| Pre-grade kill or grading hits `DockerError` | re-check `docker inspect`: exited → `graded`, failed (the agent killed PID 1 just before handing back); otherwise `infra_error` |

Consequence: drop `--rm` from `docker run` so a dead container can still be
inspected. `stop()` still removes it. Expired containers are found by label
(`harness cleanup`, built in Phase 6).

## Trial runner flow

As built today (`runner.py`; every failure point: `errors.md`):

1. Capability check: `agent.capabilities` vs `benchmark.requirements(task)`; a gap → `unsupported`, no world started (2026-10-29).
2. `agent.prepare_image(task.image)` (Claude Code: its cached derived image), then start the container (`lifetime_s` = 3 × trial timeout + 600 s margin) and the task's MCP sidecars.
3. `run_agent` with the wrapped session (logs turns, enforces the deadline, builds the transcript, records spend at each turn's end).
4. Container died? Apply rule 6.
5. Kill leftover agent processes, save `workspace.tar`.
6. Start the grader sidecar from `grader_image(task)`; `grade` (`GraderError` → `grader_error`).
7. Write `outcome.json`, `transcript.json` (the trace, `events.jsonl`, is written as it happens; mini writes its own `trajectory.json`).
8. Stop the grader, the sidecars and the container (always, in `finally`).

## Built (2026-10-29): details and deviations to review

- **Pre-grade kill is not `kill -9 -1`.** Under `--init`, `kill -9 -1` also kills the main `sleep`, the init exits with it, and the container dies before grading. `kill_agent_processes()` kills everything except PID 1 and the sleep (`environment.md` #9).
- **mini config:** `mini.yaml` (what the `mini` CLI uses: tool calls, `LitellmModel`). Its `environment.env` (PAGER=cat etc.) wasn't applied (the reasoning then: our exec has no TTY, so pagers don't page); `harness parity` flagged it as a difference from mini's own Docker mode, and since 2026-10-30 every command gets it through `exec(env=)` (`adapters.md` → Built).
- **Continuation:** mini ends a run with an `exit` message and, after a submit, an unanswered tool call. `MiniAgent.resume()` drops the `exit` marker, answers the pending tool call with the command's real output, appends the user message, and runs mini's loop again.
- **`Limits.max_turns` = 20** (new): the recording session raises `TurnLimit` past it; the runner then grades what the agent did.
- **Model-call timeout:** 120 s per call (`MODEL_TIMEOUT_S`); mini retries failed calls itself. Since 2026-10-30 each
  call (each of mini's retries too) gets at most the trial's remaining time, and none is made past the deadline:
  `TimeLimit`, which mini's retry loop now aborts on; its retry policy is otherwise unchanged.
- **`Unsupported`:** mini raises it for any non-`Text` content.
- **`AgentLimitation`** (`agents/base.py`): the agent itself can't go on. mini raises it for litellm `ContextWindowExceededError` / `ContentPolicyViolationError`; the runner ends the turns and grades the world as it is (like a timeout), reason in `agent_exit_status`.
- **Any other exception** from the agent or harness (e.g. a model API auth error, provider down, rate limit after mini's retries) → `infra_error` with the traceback. All error paths: `errors.md`.
- **Container `lifetime_s`** = 3 × trial timeout + 600 s grading margin (was trial timeout + 600 s; made generous in the 2026-10-29 audit, so it never ends before the trial does).
- **Trace on disk (Phase 4+5):** the recording session (and, since the audit, the recording environment) appends each event to `events.jsonl` (redacted) as it happens and calls the worker after each turn, which loads the file into the `events` table; `outcome.json` / `transcript.json` are written atomically and redacted, and every file in the attempt folder (e.g. mini's `trajectory.json`) is redacted after the trial.
- **Score checks:** a non-JSON `Score.value`, or one `score_to_float` can't convert → `grader_error`.

## Config (2026-10-29 audit)

- The CLI loads the repo `.env` at entry (`python-dotenv`; variables already set in the shell win), so mini's
  settings and API keys need no `set -a; . ./.env`.
- The model is resolved once, at submit: `--model`, else `HARNESS_MODEL` (one harness-wide, litellm-style setting;
  2026-10-30, was each agent's `model_env`, i.e. `MSWEA_MODEL_NAME`), and stored in `runs.model`. Each agent
  translates it with `Agent.native_model()` (mini: as is; Claude Code: without `anthropic/`). `MSWEA_MODEL_NAME`
  stays in `.env` only for the starter's `mini` CLI examples. Workers build the agent from that; adapters never read `os.environ` for it.
  No model → a clear CLI error, nothing submitted.
- `Agent.preflight()` (mini: litellm knows the model, its provider's key is set; no tokens spent) runs at submit and
  when a worker starts, before it claims anything (`errors.md`).
- `harness run` flags for the limits: `--trial-timeout` (600 s), `--cost-limit` ($1), `--max-steps` (50),
  `--memory` (2g), `--cpus` (1.0); stored in `runs.config`. `--concurrency` stays on `harness worker` and `run --wait`.

## Agent interface cleanups (2026-10-29 audit)

- `Agent` has `model` (set in its constructor, from `runs.model`) and `version()` (mini: its submodule's commit),
  so the CLI no longer assumes an agent is a submodule.
- `AgentSession.stats() -> AgentStats(exit_status, cost_usd, steps)` replaces mutable attributes (later
  `AgentStats(exit_status, cost_usd, model_calls, input_tokens, output_tokens)`, `database.md` #20); the runner no
  longer writes into the agent's session (an `AgentLimitation` is kept on the `RecordingSession`).
- A failure the harness decides (container died / out of memory during the turn) gets `score_float = 0.0` directly,
  not via `Benchmark.score_to_float`.

## mini's own retry policy is left alone (2026-10-29)

mini retries failed model calls itself (e.g. `ContentPolicyViolationError` up
to 10 times, everything not on its abort list). That is the agent's own
scaffolding, part of what a benchmark measures about the agent, so we do not
change it. The harness's retries are separate: a whole new attempt, only for
`infra_error` (`database.md` #14, `errors.md`).
