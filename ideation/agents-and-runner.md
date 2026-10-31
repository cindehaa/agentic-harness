# Agents and the trial runner (Phase 3)

**Status:** Decided 2026-10-29; built 2026-10-29 (`agents/`, `runner.py`,
`harness run`). Ownership: Claude drafts everything; Cindy reviews line by line. Later changes are marked in
place: the audit (Config, interface cleanups below), Claude Code + capabilities (`action-space.md`), cost tracking
(`database.md` #20–23), one harness-wide model (2026-10-30, Config), agents over ACP (2026-10-30, #8).

## Decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| 1 | Agent registry like `BENCHMARKS`: `AGENTS = {"mini-swe-agent": "agentic_harness.agents.mini_swe_agent:MiniSweAgent"}`, imported only when used (lazy since the 2026-10-29 audit, so `status` / `results` / benchmarks never load litellm). CLI `--agent` accepts a name or a path and uses its basename. `--model` overrides the default from `.env` (`MSWEA_MODEL_NAME`; **superseded 2026-10-30** by `HARNESS_MODEL`, see Config). Each run records agent name, model, and the agent submodule SHA (since the audit: `Agent.version()`). `claude-code` added 2026-10-29. **2026-10-30:** `AGENTS` holds only agents with code of their own (mini); every other agent is an ACP agent definition (#8), and `resolve_agent` returns what makes the agent from a model (a class, or `AcpAgent` bound to a definition) | Same pattern as benchmarks; `--agent agents/mini-swe-agent` (the brief's form) still works; SHA + model make runs reproducible | Resolving a path to code/config at runtime (more magic, nothing gained with one agent) |
| 2 | mini-swe-agent runs on the **host** via its Python API (`DefaultAgent` from `minisweagent.agents.default` + `get_model` / `LitellmModel`). An env wrapper forwards its `execute({"command"})` to our `env.exec`, raises `Submitted` when output starts with `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, and reports the container's OS in template vars | The API key never enters the container; no install step inside the image; we see every command | Installing mini inside the container (needs python + the key in the world the agent controls); driving the `mini` CLI (setup wizard, less control) |
| 3 | Multi-turn continuation built **now**: a second `send()` appends the new user message to mini's history and continues the run | Cindy wants to test it on another benchmark; the turn model already allows several `send()`s | Deferring it (one turn per trial) |
| 4 | Reply = `Message("agent", (Text(submission),))`: only the agent's own output, nothing added by the harness. mini's exit status (`Submitted` / `LimitsExceeded` / `TimeLimit`) is kept in the trial's records | The harness adds nothing to what the grader sees; the exit status is still there for debugging | — |
| 5 | Limits: the trial deadline is checked in our command wrapper (the next command after the deadline raises `TimeLimit`), plus mini's own cost/step limits and a model-call timeout. Defaults: 10 min per trial, $1 cost, 50 steps, 60 s per command. Container `lifetime_s` = trial timeout + margin (**superseded** by the audit: 3 × trial timeout + 600 s) | All limits enforced in-process with no extra machinery; the container's self-destruct timer follows the trial | Running the agent in a separate process and killing it at the deadline (more machinery) |
| 6 | Container death during the agent's turn is decided by `docker inspect` evidence (table below) | Infra failures are retried and excluded; failures the agent caused must count against it | Treating every container death as `infra_error` (an agent could game failures into excluded trials) |
| 7 | Until the DB (Phase 4), each attempt writes `runs/<run_id>/<trial_id>/attempt-<n>/` with `outcome.json`, `transcript.json`, and mini's `trajectory.json` (**superseded** by `database.md` #19f: `runs/<run_id>/<task_id>/attempt-<attempt_id>/`, plus `events.jsonl` and `workspace.tar`) | Everything inspectable on disk now; Phase 4 indexes the same files | — |
| 8 | **Agents over ACP (2026-10-30, Cindy: "scratch our adapter and just support the protocol")**: any agent that speaks the Agent Client Protocol runs through one generic adapter (`AcpAgent`, `agents/acp.py`), configured by data: built-in definitions in `agents/acp_agents.toml` (`--agent claude-code`), a private agent's own file (`--agent path/to/agent.toml`, its absolute path kept in `runs.agent`). It runs inside the world (`Environment.start_process`, `docker exec -i`); turns = `session/prompt` on one session. Details: `action-space.md` → Agents over ACP | No per-agent code in our repo: a vendor ships a definition (and its install image), not a fork; Claude Code, Codex, Gemini CLI, Copilot, goose, OpenCode, ... already speak ACP | Keeping a dedicated adapter per agent (each one parses its own log format and flags) |

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
2. `agent.prepare_image(task.image)` (an ACP agent: the task's image + the agent's install, cached), then start the container (`lifetime_s` = 3 × trial timeout + grade timeout + 600 s margin) and the task's MCP sidecars.
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
- **Container `lifetime_s`** = 3 × trial timeout + grade timeout (since 2026-10-31) + 600 s margin (was trial timeout + 600 s; made generous in the 2026-10-29 audit, so it never ends before the trial does).
- **Trace on disk (Phase 4+5):** the recording session (and, since the audit, the recording environment) appends each event to `events.jsonl` (redacted) as it happens and calls the worker after each turn, which loads the file into the `events` table; `outcome.json` / `transcript.json` are written atomically and redacted, and every file in the attempt folder (e.g. mini's `trajectory.json`) is redacted after the trial.
- **Score checks:** a non-JSON `Score.value`, or one `score_to_float` can't convert → `grader_error`.

## Config (2026-10-29 audit)

- The CLI loads the repo `.env` at entry (`python-dotenv`; variables already set in the shell win), so mini's
  settings and API keys need no `set -a; . ./.env`.
- The model is resolved once, at submit: `--model`, else `HARNESS_MODEL` (one harness-wide, litellm-style setting;
  2026-10-30, was each agent's `model_env`, i.e. `MSWEA_MODEL_NAME`), and stored in `runs.model`. Each agent
  translates it with `Agent.native_model()` (mini: as is; an ACP agent: its definition's `model_prefix` removed, e.g. Claude Code without `anthropic/`, and passed in its `model_env`, e.g. `ANTHROPIC_MODEL`). `MSWEA_MODEL_NAME`
  stays in `.env` only for the starter's `mini` CLI examples. Workers build the agent from that; adapters never read `os.environ` for it.
  No model → a clear CLI error, nothing submitted.
- `Agent.preflight()` (mini: litellm knows the model, its provider's key is set; no tokens spent) runs at submit and,
  in each worker, once per run before it claims any of that run's work; a run that fails it fails alone (2026-10-30,
  `errors.md`; was: the worker refused to start).
- `harness run` flags for the limits: `--trial-timeout` (600 s), `--cost-limit` ($1), `--max-steps` (50),
  `--memory` (2g), `--cpus` (1.0); stored in `runs.config`. **2026-10-31:** they override only when given, over what
  the benchmark declares (Limits in three layers, below); `--grade-timeout` (120 s) added. `--concurrency` stays on `harness worker` and `run --wait`.

## Limits in three layers (Cindy, 2026-10-30; built 2026-10-31)

| # | Decision | Why | Rejected |
|---|---|---|---|
| L1 | Per trial: harness defaults (`Limits`, in `runs.config.limits`) < what the benchmark declares for the task (`Benchmark.limits(task) -> LimitOverrides`, only the fields it sets; default none) < flags given explicitly (`runs.config.limit_flags`; a flag left out is `None`, never a default). Resolved in `run_trial` (`effective_limits`), so the worker, `validate` and tests all get the benchmark's own | A benchmark's own timeouts are part of what it measures; the user can still override them on purpose | Defaults that silently override a benchmark's values (Terminal-Bench's 900 s agent timeout ran at 600 s); click defaults (can't tell "given" from "default") |
| L2 | A flag replacing a *different* value the benchmark declares for one of the run's tasks makes the run non-standard: `runs.config.non_standard_limits` (names), a note at submit, in `results` summary/table and on the dashboard | Its scores aren't comparable with the benchmark's standard setup, and the reader must see that | Refusing such flags (overriding is legitimate, e.g. a quick smoke run) |
| L3 | `Limits.grade_timeout_s` (120 s): `grade()` gets its grader and world wrapped (`GradingDeadline`); every command is capped to what is left (no timeout = the rest), one started after it → `GraderError`. Container lifetime = 3 × trial timeout + grade timeout + 600 s | Grading had per-adapter timeouts (bash-operations 60 s exec default, python-tasks 120 s, Terminal-Bench its own); one harness-wide, overridable timeout is enforced the same way for all | A timer around `grade()` (can't stop a command already running; threads) |
| L4 | Operator controls (`--max-running`, `--concurrency`, `--max-run-cost`) are not `Limits` fields, so `LimitOverrides` can't carry them | They describe the operator's machine and wallet, not the task | — |
| L5 | Each attempt records its effective limits with their source (`default` / `benchmark` / `flag`) in `outcome.json` and `attempts.limits` (schema v5); a limit the agent can't keep is `"enforced": false` (`Agent.unenforced_limits()`), never a fake count | A result must say what it ran under | Recording only the run's flags (per-task values differ) |
| L6 | Terminal-Bench: `max_agent_timeout_sec` → trial timeout, `max_test_timeout_sec` → grade timeout (Terminal-Bench's defaults 360 / 60 s when absent); its task.yaml has no memory/CPU fields. python-tasks declares nothing: its grader stops pytest at 30 s, under the 120 s default | Faithful to how Terminal-Bench runs | — |
| L7 | ACP agents get limits they keep themselves through their definition (`limits_meta`: a limit → a dotted path in `session/new`'s `_meta`). Claude Code: `max_steps` → `claudeCode.options.maxTurns` (claude-agent-acp hands those options to the Claude Agent SDK). Verified live: maxTurns 1 stops a tool-using prompt after one API round trip and is counted **per prompt** (the next prompt runs), so a multi-turn benchmark gets it per turn. Claude Code reports reaching it as a JSON-RPC error ("Reached maximum number of turns"): the turn ends `LimitsExceeded`, graded. Without `limits_meta`, `max_steps` is recorded not enforced; `command_timeout_s` always is for ACP agents (they run their own commands) | Data, not code; no fake step counts | Counting tool calls as steps (not model calls) |

## Agent interface cleanups (2026-10-29 audit)

- `Agent` has `model` (set in its constructor, from `runs.model`) and `version()` (mini: its submodule's commit),
  so the CLI no longer assumes an agent is a submodule.
- `AgentSession.stats() -> AgentStats(exit_status, cost_usd, steps)` replaces mutable attributes (later
  `AgentStats(exit_status, cost_usd, model_calls, input_tokens, output_tokens)`, `database.md` #20); the runner no
  longer writes into the agent's session (an `AgentLimitation` is kept on the `RecordingSession`).
- A failure the harness decides (container died / out of memory during the turn) gets `score_float = 0.0` directly,
  not via `Benchmark.score_to_float`.

## `cap_model_calls` stays in the mini adapter (Cindy, 2026-10-31)

Correctness doesn't need it: `ContainerEnv.execute` refuses commands past the deadline. It exists because the trial
timeout is a wall-clock limit for every agent (ACP agents get cancel-then-kill): without it, mini's own retries (up to
10 tries × 120 s plus backoff, `agents/mini-swe-agent/src/minisweagent/models/utils/retry.py`) can run ~25 min past a
10-min deadline during a provider outage. Rejected: lowering `MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT` (cheaper, but it
changes mini's own behaviour).

## mini's own retry policy is left alone (2026-10-29)

mini retries failed model calls itself (e.g. `ContentPolicyViolationError` up
to 10 times, everything not on its abort list). That is the agent's own
scaffolding, part of what a benchmark measures about the agent, so we do not
change it. The harness's retries are separate: a whole new attempt, only for
`infra_error` (`database.md` #14, `errors.md`).
