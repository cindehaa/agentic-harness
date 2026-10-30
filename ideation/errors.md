# Error paths in a trial

**Last updated:** 2026-10-29

A living doc: every place a trial can fail, and which outcome it ends in.
Conditions are the code's own (`runner.py`, `worker.py`, `store.py`,
`agents/mini_swe_agent.py`, `environments/docker.py`). The runner sees only the `Environment`
port: `EnvironmentLost` (Docker raises its subclass `DockerError`) and `env.state()`
(`EnvironmentState`: running, exit code, OOM-killed; None = gone). Outcome statuses: `task-input.md` → Outcome model.
Update this whenever error handling changes.

```mermaid
flowchart TB
  setup["worker (first to claim runs.setup_claimed_by): Benchmark.setup(tasks) builds the images, once per run"]
  setup -->|"raises"| abort["run failed (traceback + build log in runs/run/setup.log); every trial done as infra_error (not retried)"]
  setup -->|"setup worker's lease expires"| setup
  setup --> claim["a worker claims a trial (lease 120 s, renewed every 30 s)"]
  claim --> startenv["run_trial: env_factory(image, lifetime_s, limits).start(), DockerEnvironment injected by the worker (attempt row + folder attempt-id created with the claim)"]
  claim -->|"lease expires: worker killed or hung"| lost["another worker reclaims it: the open attempt is closed as infra_error (worker lost), counts as an attempt"]
  startenv -->|"EnvironmentLost (DockerError): missing image, docker run fails, Docker down"| IE1
  startenv --> turns["benchmark.run_agent(task, session, env)"]

  turns -->|"Unsupported: content the agent can't read, e.g. a File"| UN["unsupported"]
  turns -->|"returns: mini exit Submitted, LimitsExceeded (steps, cost), TimeLimit"| check
  turns -->|"TurnLimit: more than max_turns messages"| check
  turns -->|"AgentLimitation: ContextWindowExceededError, ContentPolicyViolationError"| check
  turns -->|"EnvironmentLost"| check
  turns -->|"any other exception: auth, permission, bad request, provider down, rate limit after mini's retries, adapter or benchmark bug"| IE2["infra_error (traceback saved)"]

  check{"after the turn: env.state() (Docker: docker inspect)"}
  check -->|"None (vanished, Docker unreachable)"| IE3
  check -->|"exited, OOMKilled: over our --memory limit (Limits.memory, 2g)"| GF
  check -->|"exited, exit code 0, not OOMKilled: the keep-alive's lifetime (3 x trial timeout + 600 s) ran out"| IE3
  check -->|"exited, non-zero exit code: something inside killed the main process"| GF
  check -->|"Running, but the turn raised EnvironmentLost"| IE3
  check -->|"Running"| kill["env.kill_agent_processes()"]

  kill --> grade["benchmark.grade(task, env, transcript)"]
  kill -->|"EnvironmentLost"| recheck
  grade -->|"EnvironmentLost"| recheck{"env.state() again (the agent may have killed PID 1 just before handing back)"}
  recheck -->|"None"| IE3
  recheck -->|"exited, OOMKilled"| GF
  recheck -->|"exited, exit code 0"| IE3
  recheck -->|"exited, non-zero"| GF
  recheck -->|"Running"| IE3
  grade -->|"GraderSetupError: installing the grader's deps failed (e.g. apk add, no network)"| IE4["infra_error (grader setup)"]
  grade -->|"GraderError: grader crashed, unparseable output"| GE["grader_error"]
  grade -->|"any other exception"| IE2
  grade -->|"Score"| usable{"json.dumps(value) and score_to_float(score) succeed?"}
  usable -->|"no: TypeError, ValueError"| GE
  usable -->|"yes"| G["graded (its score)"]

  GF["graded: Score(False, container died / ran out of memory during agent turn), score_float 0"]
  IE1["infra_error"]
  IE3["infra_error (container lost)"]

  IE1 --> retry{"fewer than 3 attempts?"}
  IE2 --> retry
  IE3 --> retry
  IE4 --> retry
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
  class IE1,IE2,IE3,IE4,lost,final,abort,lie infraerr
  class UN,lun unsupported
  class lpl planned
```

Retries, leases and queue-mode setup: Phase 4+5 (`database.md` #12–14).

| Outcome | Counts toward accuracy? | Retried? | Whose fault |
|---|---|---|---|
| `graded` | yes (its `score_float`) | no | nobody's: the agent's result, including failures it caused (killed its container, hit a limit, context full, model refused) |
| `grader_error` | no; reported prominently (count + list) | no | most likely the grader or the benchmark adapter |
| `infra_error` | no | yes, up to 3 attempts in total (backoff 5 s × attempt number); a setup failure is not retried | ours: harness, Docker, or our model-provider setup (key, quota, outage) |
| `unsupported` | no; reported separately | no | a capability gap: the agent can't take this task's content |

## Notes

- **How mini surfaces model errors.** litellm raises; mini's `LitellmModel.query`
  retries (up to 10 attempts, `MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT`) except for
  its abort list (`AuthenticationError`, `PermissionDeniedError`, `NotFoundError`,
  `UnsupportedParamsError`, `ContextWindowExceededError`), then re-raises the
  original exception after logging an `exit` message whose `exit_status` is the
  class name. `ContentPolicyViolationError` is not on the abort list, so mini
  retries it before it reaches us. Our adapter turns the two limitation errors
  into `AgentLimitation`; everything else passes through to the runner's
  catch-all (`infra_error` with the traceback).
- **Grader deps need the network.** bash-operations installs `python3` with
  `apk` at grade time. If that install fails (e.g. no network) the adapter
  raises `GraderSetupError` → `infra_error`, retried: the infrastructure
  failed, not the grader (fix A, 2026-10-29). Phase 6 option B removes the
  network need (`environment-and-grading.md`, known gaps).
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
