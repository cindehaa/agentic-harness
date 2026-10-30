# Error paths in a trial

**Last updated:** 2026-10-29

A living doc: every place a trial can fail, and which outcome it ends in.
Conditions are the code's own (`runner.py`, `agents/mini_swe_agent.py`,
`environments/docker.py`). Outcome statuses: `task-input.md` → Outcome model.
Update this whenever error handling changes.

```mermaid
flowchart TB
  setup["harness run: Benchmark.setup(tasks) builds the images, once per run"]
  setup -->|"raises"| abort["harness run stops before any trial (no outcomes)"]
  setup --> startenv["run_trial: DockerEnvironment.start()"]
  startenv -->|"DockerError: missing image, docker run fails, Docker down"| IE1
  startenv --> turns["benchmark.run_agent(task, session, env)"]

  turns -->|"Unsupported: content the agent can't read, e.g. a File"| UN["unsupported"]
  turns -->|"returns: mini exit Submitted, LimitsExceeded (steps, cost), TimeLimit"| check
  turns -->|"TurnLimit: more than max_turns messages"| check
  turns -->|"AgentLimitation: ContextWindowExceededError, ContentPolicyViolationError"| check
  turns -->|"DockerError"| check
  turns -->|"any other exception: auth, permission, bad request, provider down, rate limit after mini's retries, adapter or benchmark bug"| IE2["infra_error (traceback saved)"]

  check{"after the turn: env.state() (docker inspect)"}
  check -->|"None (vanished, Docker unreachable) or OOMKilled"| IE3
  check -->|"exited (not Running)"| GF
  check -->|"Running, but the turn raised DockerError"| IE3
  check -->|"Running"| kill["env.kill_agent_processes()"]

  kill --> grade["benchmark.grade(task, env, transcript)"]
  kill -->|"DockerError"| recheck
  grade -->|"DockerError"| recheck{"env.state() again (the agent may have killed PID 1 just before handing back)"}
  recheck -->|"None or OOMKilled"| IE3
  recheck -->|"exited"| GF
  recheck -->|"Running"| IE3
  grade -->|"GraderError: grader crashed, grader deps install failed, unparseable output"| GE["grader_error"]
  grade -->|"any other exception"| IE2
  grade -->|"Score"| usable{"json.dumps(value) and score_to_float(score) succeed?"}
  usable -->|"no: TypeError, ValueError"| GE
  usable -->|"yes"| G["graded (its score)"]

  GF["graded: Score(False, container died during agent turn)"]
  IE1["infra_error"]
  IE3["infra_error (container lost)"]

  IE1 -.-> retry["retry as a new attempt (Phase 5, max attempts TBD)"]
  IE2 -.-> retry
  IE3 -.-> retry
  retry -.-> startenv

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
  class IE1,IE2,IE3,lie infraerr
  class UN,lun unsupported
  class retry,lpl planned
```

Dashed arrows and the dashed gray node are planned (Phase 5), not built.

| Outcome | Counts toward accuracy? | Retried? | Whose fault |
|---|---|---|---|
| `graded` | yes (its `score_float`) | no | nobody's: the agent's result, including failures it caused (killed its container, hit a limit, context full, model refused) |
| `grader_error` | no; reported prominently (count + list) | no | most likely the grader or the benchmark adapter |
| `infra_error` | no | yes (Phase 5, planned; max attempts TBD) | ours: harness, Docker, or our model-provider setup (key, quota, outage) |
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
  `apk` at grade time; with no network that is a `GraderError` → `grader_error`.
