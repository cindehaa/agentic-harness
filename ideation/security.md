# Security: hidden grader assets and credentials

**Status:** Decided 2026-10-29. Design only; nothing built yet.

## (a) Hidden grading assets, METR-style (pronounced "meter")

The agent must not read or tamper with grader files and answers in the
shared trial container.

- An unprivileged `agent` user is created at image build.
- The agent's commands run via `docker exec -u agent`.
- Grader files are root-only (`chmod 700`); grading runs as root.
- Before grading, leftover agent processes are killed: `pkill -9 -u agent`
  when the agent runs as the unprivileged user; `Environment.kill_agent_processes()`
  (every process except PID 1 and its main `sleep`; a plain `kill -9 -1` would
  kill the container too) when `agent_user` is root (our two benchmarks).
- **`docker cp` keeps the host's uid and mode bits**, so grader files uploaded
  with `Environment.upload` are not root-only by default. After upload, run an
  explicit `chown -R root:root <path>` + `chmod 700 <path>` (as root).
- **Caveat:** permissions hide files, not installed tools. Grader-only tools go
  in a root-only prefix; final grading can still install them at grade time.
- Each adapter declares `agent_user`. Default `"root"` for fidelity
  (Terminal-Bench tasks expect root).
- **Our two benchmarks:** root; grader files go only into the attached grader sidecar, never into the
  agent's world (2026-10-29, `environment-and-grading.md` #7). With a sidecar, the permission scheme
  above is needed only for tools a grader must run inside the world itself.
- `Environment.exec` gains a `user` parameter when first needed.

## (b) Credentials

Four kinds, by who should see them:

| Kind | Who sees it |
|---|---|
| Agent's model key | the agent only |
| Judge / simulated-user keys | host only |
| Infra creds (registry, cloud) | host only; never baked into images |
| Task credentials | fake; part of the world data |

The code needs only two mechanisms:

1. **Allowlist at the container boundary.** Agent config `forward_env` names the
   variables an agent may get; values are injected at runtime with
   `docker exec -e`, never written into image layers.
2. **Redaction at the log boundary.** The recorder replaces every `.env` value
   with `***`.

mini-swe-agent runs on the host, so its key never enters a container.

**Built 2026-10-29** (Claude Code, `action-space.md` #4): `ClaudeCode.forward_env = ("ANTHROPIC_API_KEY",)`;
`Environment.exec(env=...)` passes it as `docker exec -e NAME` with the value in the docker client's own environment,
so it is never on the host's command line nor in an image layer; the command event records names only (`***`); every
attempt file is redacted. Cost: the key is inside the agent's world while Claude Code runs, so the agent could read it.
`exec(user=)` is built too (for the unprivileged `agent` user), not used yet.

## (c) Judge model

Default to the benchmark's prescribed model, overridable. Recorded in
`Score.metadata`; judge cost tracked separately from agent cost.

## (d) Multi-step intermediate grading

Uses the same METR mechanism: intermediate checks run as root between turns,
grader assets stay hidden from the `agent` user. Design only.
