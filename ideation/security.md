# Security: hidden grader assets and credentials

**Status:** Decided 2026-10-29. Built: credentials (b) (allowlist + redaction) and `exec(user=)`. Not built:
the unprivileged `agent` user and `agent_user` (a), network (designed 2026-10-30), judge model (c), intermediate
grading (d).

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
  in a root-only prefix. (Installing them at grade time was superseded 2026-10-29: the grader sidecar's own
  image carries them, `environment-and-grading.md` #7.)
- Each adapter declares `agent_user`. Default `"root"` for fidelity
  (Terminal-Bench tasks expect root).
- **Our two benchmarks:** root; grader files go only into the attached grader sidecar, never into the
  agent's world (2026-10-29, `environment-and-grading.md` #7). With a sidecar, the permission scheme
  above is needed only for tools a grader must run inside the world itself.
- `Environment.exec` gains a `user` parameter when first needed. (Built 2026-10-29 with Claude Code; nothing
  passes it yet, since no adapter declares a non-root `agent_user`.)

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
2. **Redaction at the log boundary.** The recorder replaces `.env` values
   with `***` (built: values whose key contains KEY / TOKEN / SECRET / PASSWORD and that are ≥ 8 characters,
   `redact.py`, `database.md` #17). Since 2026-10-30 also secret-named variables in the harness's environment (a key
   exported in the shell, not in `.env`) and every forwarded variable (`forward_env`), whatever its name.

mini-swe-agent runs on the host, so its key never enters a container.

**Built 2026-10-29** (Claude Code, `action-space.md`), kept for ACP agents (2026-10-30): an agent definition's
`forward_env` (claude-code: `ANTHROPIC_API_KEY`) is passed as `docker exec -e NAME` (`exec(env=...)`, and
`start_process(env=...)` for the agent's own process) with the value in the docker client's own environment, so it
is never on the host's command line nor in an image layer; the `command` / `process` events record names only
(`***`); every attempt file (incl. the raw ACP log `acp.jsonl`) is redacted. Cost: the key is inside the agent's world
while an in-world agent runs, so the agent could read it.
`exec(user=)` is built too (for the unprivileged `agent` user), not used yet.

## Network (designed 2026-10-30, not built)

Worlds have internet today. A benchmark will declare `network: bool` (default on; `environment.md` → Network); an
offline one runs the trial container without internet, so the agent can neither look answers up nor send out
what it finds. The in-world agent's API key is the gap: an in-world (ACP) agent must still reach the model API, so an offline
world gets a model-API-only allowlist (Harbor's `network_allowlist`) or a model proxy that holds the key on the host
(Inspect's approach; the key would then never enter the world either).

## (c) Judge model

Default to the benchmark's prescribed model, overridable. Recorded in
`Score.metadata`; judge cost tracked separately from agent cost.

## (d) Multi-step intermediate grading

Uses the same METR mechanism: intermediate checks run as root between turns,
grader assets stay hidden from the `agent` user. Design only.
