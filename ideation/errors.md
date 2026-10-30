# Error paths in a trial

**Last updated:** 2026-10-29

A living doc: every place a trial can fail, and which outcome it ends in.
Conditions are the code's own (`runner.py`, `worker.py`, `store.py`,
`agents/mini_swe_agent.py`, `environments/docker.py`). The runner sees only the `Environment`
port: `EnvironmentLost` (Docker raises its subclass `DockerError`) and `env.state()`
(`EnvironmentState`: running + `EndReason` once ended: EXPIRED / RESOURCE_LIMIT / KILLED / UNKNOWN; None = gone; the backend translates its own evidence, Docker: `_end_reason()`). Outcome statuses: `task-input.md` → Outcome model.
Update this whenever error handling changes.

```mermaid
flowchart TB
  setup["worker (first to claim runs.setup_claimed_by): Benchmark.setup(tasks) builds the images, once per run"]
  setup -->|"raises"| abort["run failed (traceback + build log in runs/run/setup.log); every trial done as infra_error (not retried)"]
  setup -->|"setup worker's lease expires"| setup
  preflight["worker start (and harness run at submit): Agent.preflight() for every unfinished run: model known to litellm, its API key set; no tokens spent"]
  preflight -->|"PreflightError"| refuse["worker refuses to start, names the run; nothing claimed"]
  preflight --> setup
  setup --> claim["a worker claims a trial (lease 120 s, renewed every 30 s)"]
  claim --> caps{"run_trial: agent.capabilities.missing(benchmark.requirements(task)): content types, multi-turn, MCP"}
  caps -->|"a gap (e.g. mini on a task with MCP servers)"| UN
  caps --> prep["agent.prepare_image(task.image): Claude Code builds its cached derived image"]
  prep -->|"DockerError (build failed, Docker down)"| IE1
  prep --> startenv["env_factory(image, lifetime_s, limits).start(), DockerEnvironment injected by the worker (attempt row + folder attempt-id created with the claim)"]
  claim -->|"lease expires: worker killed or hung"| lost["another worker reclaims it: the open attempt is closed as infra_error (worker lost), counts as an attempt"]
  startenv -->|"EnvironmentLost (DockerError): missing image, docker run fails, Docker down"| IE1
  startenv --> mcp["start each of benchmark.mcp_servers(task) as a network-only sidecar; ready = port LISTEN"]
  mcp -->|"EnvironmentLost: image missing, not listening within 60 s (error has its logs)"| IE1
  mcp --> turns["benchmark.run_agent(task, session, env)"]

  turns -->|"Unsupported: content the agent can't read, e.g. a File"| UN["unsupported"]
  turns -->|"returns: mini exit Submitted, LimitsExceeded (steps, cost), TimeLimit; Claude Code exit success, error_max_turns, error_max_budget_usd, TimeLimit (exec timeout: claude killed inside)"| check
  turns -->|"TurnLimit: more than max_turns messages"| check
  turns -->|"AgentLimitation: ContextWindowExceededError, ContentPolicyViolationError; Claude Code prompt_too_long, refusal"| check
  turns -->|"EnvironmentLost"| check
  turns -->|"TransientError (mini: litellm APIConnectionError incl. timeouts, RateLimitError, InternalServerError incl. overloaded, ServiceUnavailableError, BadGatewayError, after mini's own retries; Claude Code rate_limit, server_error, overloaded, HTTP 408/429/5xx/529 after its own retries), ConnectionError, TimeoutError"| IE5["infra_error, retryable (traceback saved)"]
  turns -->|"any other exception: auth, permission, bad request, adapter or benchmark bug; ClaudeCodeError (authentication_failed, billing_error, model_not_found, invalid_request, no result event, claude never ran)"| IE2["infra_error, NOT retryable: fails fast, logged as an error (traceback saved)"]

  check{"after the turn: env.state() (Docker: docker inspect)"}
  check -->|"None (vanished, Docker unreachable)"| IE3
  check -->|"RESOURCE_LIMIT: over limits we set (Docker: OOMKilled under --memory, Limits.memory 2g)"| GF
  check -->|"EXPIRED: the keep-alive's lifetime (3 x trial timeout + 600 s) ran out (Docker: exited 0, not OOMKilled)"| IE3
  check -->|"KILLED: something inside ended the main process (Docker: exited non-zero)"| GF
  check -->|"UNKNOWN: the backend can't tell (Docker: Status not exited, e.g. dead)"| IE3
  check -->|"Running, but the turn raised EnvironmentLost"| IE3
  check -->|"Running"| kill["env.kill_agent_processes()"]

  kill --> save["save workspace.tar (size-capped tar via exec); OSError (e.g. workspace deleted) is noted in the trace, not fatal"]
  save --> gstart["start the attached grader: env.attach_grader(benchmark.grader_image(task)) (shares the world's workspace, network, processes)"]
  save -->|"EnvironmentLost"| recheck
  gstart -->|"EnvironmentLost (DockerError): grader image missing, docker run fails"| recheck
  gstart --> grade["benchmark.grade(task, grader, env, transcript); then the grader is stopped, then the world"]
  kill -->|"EnvironmentLost"| recheck
  grade -->|"EnvironmentLost"| recheck{"env.state() again (the agent may have killed PID 1 just before handing back)"}
  recheck -->|"None"| IE3
  recheck -->|"RESOURCE_LIMIT, KILLED"| GF
  recheck -->|"EXPIRED, UNKNOWN"| IE3
  recheck -->|"Running"| IE3
  grade -->|"GraderError: grader crashed, unparseable output"| GE["grader_error"]
  grade -->|"any other exception"| IE2
  grade -->|"Score"| usable{"json.dumps(value) and score_to_float(score) succeed?"}
  usable -->|"no: TypeError, ValueError"| GE
  usable -->|"yes"| G["graded (its score)"]

  GF["graded: Score(False, container died / exceeded its resource limits during agent turn), score_float 0"]
  IE1["infra_error"]
  IE3["infra_error (container lost)"]

  IE1 --> retry{"fewer than 3 attempts?"}
  IE2 --> final
  IE5 --> retry
  IE3 --> retry
  lost --> retry
  retry -->|"yes: back to pending, available_at = now + 5 s x attempt number"| claim
  retry -->|"no"| final["trial ends infra_error"]

  subgraph legend["Legend"]
    direction LR
    lg["graded"]
    lge["grader_error"]
    lie["infra_error"]
    lun["unsupported"]
    lpl["planned (not built)"]
  end

  classDef graded fill:#d3f0d3,stroke:#1e5e1e,color:#1e5e1e
  classDef gradererr fill:#ffe0a8,stroke:#8a5a00,color:#6b4500
  classDef infraerr fill:#f8d0d0,stroke:#7a1f1f,color:#7a1f1f
  classDef unsupported fill:#e0e0e0,stroke:#666,color:#444
  classDef planned fill:#f5f5f5,stroke:#999,color:#777,stroke-dasharray:4 3

  class G,GF,lg graded
  class GE,lge gradererr
  class IE1,IE2,IE3,IE5,lost,final,abort,refuse,lie infraerr
  class UN,lun unsupported
  class lpl planned
```

Retries, leases and queue-mode setup: Phase 4+5 (`database.md` #12–14).

| Outcome | Counts toward accuracy? | Retried? | Whose fault |
|---|---|---|---|
| `graded` | yes (its `score_float`) | no | nobody's: the agent's result, including failures it caused (killed its container, hit a limit, context full, model refused) |
| `grader_error` | no; reported prominently (count + list) | no | most likely the grader or the benchmark adapter |
| `infra_error` | no | only if `retryable` (2026-10-29 audit): environment lost / Docker errors, network, provider 5xx / overloaded / rate limit, grader failed to start, container lost or lifetime expired, worker lost: up to 3 attempts in total (backoff 5 s × attempt number). Not retried (fail fast, logged as an error): auth / permission, bad request / misconfiguration, any unexpected exception in harness or adapter code; a setup failure | ours: harness, Docker, or our model-provider setup (key, quota, outage) |
| `unsupported` | no; reported separately | no | a capability gap: `AgentCapabilities` vs `Benchmark.requirements(task)` (content types, multi-turn, MCP), checked once before the world starts; or the agent raised `Unsupported` for a message it can't read |

## Notes

- **How mini surfaces model errors.** litellm raises; mini's `LitellmModel.query`
  retries (up to 10 attempts, `MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT`) except for
  its abort list (`AuthenticationError`, `PermissionDeniedError`, `NotFoundError`,
  `UnsupportedParamsError`, `ContextWindowExceededError`), then re-raises the
  original exception after logging an `exit` message whose `exit_status` is the
  class name. `ContentPolicyViolationError` is not on the abort list, so mini
  retries it before it reaches us. Our adapter turns the two limitation errors
  into `AgentLimitation` and the transient ones (connection, timeout, rate limit,
  overloaded, 5xx) into `TransientError` (retryable `infra_error`); everything
  else passes through to the runner's catch-all (`infra_error`, not retryable,
  with the traceback).
- **The grader runs in an attached sidecar** (2026-10-29,
  `environment-and-grading.md` #7) from `Benchmark.grader_image(task)`, pre-pulled in
  `setup()`. Nothing is installed at grade time, so grading needs no network and
  `GraderSetupError` is gone (was: `apk add python3` failing → `infra_error`). If the
  grader can't start, that is `EnvironmentLost` → the world's state is checked again
  (rule 6), and if the world still runs → `infra_error`, retried.
- **Claude Code's errors** (2026-10-29, `action-space.md` #9) come from its stream-json: the result event's
  `terminal_reason: api_error` and the failed message's `error` kind, classified like mini's litellm errors. Its own
  model-call retries are left alone too. The trace it produced before failing is still written
  (`AgentSession.new_events`, read in a `finally`).
- **mini's own retries are left alone.** mini retries failed model calls
  itself (e.g. `ContentPolicyViolationError` up to 10 times). That is the
  agent's scaffolding, part of what is measured; we don't change it
  (`agents-and-runner.md`). Our retries are whole attempts, for `infra_error` only.
- **Worker lost.** A worker renews its trial's lease every 30 s. If it is
  killed (or hangs) the lease expires after 120 s; the next worker to look
  closes the open attempt as `infra_error` ("worker lost") and re-queues the
  trial (or ends it `infra_error` after 3 attempts). The lost attempt counts
  toward the 3, so a trial that kills its worker every time still ends. A late
  write from the lost worker is discarded: finishing an attempt is fenced on
  `claimed_by` + `attempt_count` (`database.md` #19). The heartbeat retries a
  renewal that hit "database is locked". Setup has the same lease: a dead
  setup worker's run is re-claimed by another worker.
