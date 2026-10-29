# Security: hidden grader assets and credentials

**Status:** Decided 2026-10-29. Design only; nothing built yet.

## (a) Hidden grading assets, METR-style (pronounced "meter")

The agent must not read or tamper with grader files and answers in the
shared trial container.

- An unprivileged `agent` user is created at image build.
- The agent's commands run via `docker exec -u agent`.
- Grader files are root-only (`chmod 700`); grading runs as root.
- Before grading, leftover agent processes are killed: `pkill -9 -u agent`
  when the agent runs as the unprivileged user; `kill -9 -1` when `agent_user`
  is root (our two benchmarks).
- **`docker cp` keeps the host's uid and mode bits**, so grader files uploaded
  with `Environment.upload` are not root-only by default. After upload, run an
  explicit `chown -R root:root <path>` + `chmod 700 <path>` (as root).
- **Caveat:** permissions hide files, not installed tools. Grader-only tools go
  in a root-only prefix; final grading can still install them at grade time.
- Each adapter declares `agent_user`. Default `"root"` for fidelity
  (Terminal-Bench tasks expect root).
- **Our two benchmarks:** root + grader files copied in at grade time.
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

## (c) Judge model

Default to the benchmark's prescribed model, overridable. Recorded in
`Score.metadata`; judge cost tracked separately from agent cost.

## (d) Multi-step intermediate grading

Uses the same METR mechanism: intermediate checks run as root between turns,
grader assets stay hidden from the `agent` user. Design only.
