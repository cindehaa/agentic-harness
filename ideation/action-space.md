# How agents act on the world (action space), MCP, and the Claude Code adapter

**Status:** Decided 2026-10-29 (locked by Cindy); built 2026-10-29: `AgentCapabilities`, MCP sidecars,
`exec(env=, user=)`, `ClaudeCode` (`agents/claude_code.py`). Not built: τ²-bench, Python function-calling agents,
GUI actions.

## Survey: four patterns

| Pattern | What the agent emits | Examples | Fits |
|---|---|---|---|
| Shell exec | a command string; the harness runs it in the world | mini-swe-agent, Terminal-Bench / Harbor | anything with a shell; zero per-benchmark code |
| Typed actions | actions from a fixed, harness-defined set (`click(x,y)`, `edit_file`) | SWE-agent's ACI, OpenHands actions, BrowserGym / WebArena, OSWorld | one environment family; the harness owns the action set |
| Function tools | JSON calls against function schemas the harness passes to the model | Inspect tools, verifiers tool environments, τ-bench domain tools | Python agents that call the model API themselves |
| MCP | tool calls over the Model Context Protocol to servers the task declares | Harbor `mcp_servers` (task.toml, written into the agent's MCP config), Inspect `mcp_tools` + `sandbox_agent_bridge`, verifiers Toolsets exposed as MCP | black-box agents that already speak MCP (Claude Code, Codex, ...) |

## The converged design (ours)

Agents act through **(a) their own native tools reaching the world** (shell, files: whatever the agent ships with)
and **(b) benchmark-specific actions served by MCP servers the task declares**. Harbor, Inspect and verifiers converged
on this: the benchmark owns its actions once (one MCP server), and every MCP-speaking agent gets them without
per-agent glue.

- `Benchmark.mcp_servers(task) -> list[McpServer]` (name, image, command, port; streamable HTTP). Default `[]`.
- The runner starts each as an **attached sidecar** (`Environment.attach(Sidecar(..., shares_world=False))`): it joins
  the trial's network namespace only, so the agent's world reaches it at `http://localhost:<port>/mcp`, but never
  its files or processes (a shared PID namespace would let the agent read the server through `/proc/<pid>/root`).
  Started before the agent's turn, stopped with the trial (still up while grading). The grader sidecar uses the same
  mechanism with `shares_world=True` (`attach_grader(image)` is now `attach(Sidecar(image))`).
- A service sidecar's main process is still the keep-alive `sleep` (the self-destruct timer); the server runs beside it
  (`docker exec -d`, output to PID 1's so `docker logs` shows it). `start()` returns once the port is LISTEN in
  `/proc/net/tcp` (no curl/nc needed in any image); not listening in 60 s → `EnvironmentLost` (retryable `infra_error`).
- **Python function-calling agents** (design only, not built): convert the MCP servers' tools to function schemas
  and call them through an MCP client on the host.
- **Precedent:** Harbor's tau3 adapter serves τ³'s domain tools from an MCP sidecar. τ²-bench for us: designed, not
  built (`implementation-plan.md` → Phase 7).

## One capability check, in one place

`AgentCapabilities(content_types, multi_turn, mcp)` is declared by each agent adapter; `Benchmark.requirements(task)`
says what a task needs (default: text, single turn, `mcp` iff it declares MCP servers). The runner compares them
**once, before the world starts** (`agent.capabilities.missing(...)`); a gap → `unsupported`. No scattered
`if agent.supports_x`. Agents still raise `Unsupported` if a later message carries content they can't read.

- **mini-swe-agent stays bash-only**: `content_types={"text"}, multi_turn=True, mcp=False`. No MCP shim, no prompt
  changes; a task needing MCP is `unsupported` for it.
- **Claude Code**: `content_types={"text"}` (images later), `multi_turn=True`, `mcp=True`.

## Still open industry-wide (talking points)

- **GUI / desktop**: no standard action protocol. Each harness ships its own (screenshots + coordinates, accessibility
  trees, VNC); an MCP computer-use server is possible but nothing is settled.
- **Uniform trace for black-box agents**: intercept model traffic (a proxy between the agent and the model API, as
  Inspect's `sandbox_agent_bridge` does). Every agent's trace then has the same shape (requests, responses, tool
  calls), whatever it logs itself. Today we parse each agent's own log instead (Claude Code: stream-json).
- **Simulated users** (τ-bench style), three ways: as a tool the agent calls; as a second agent the benchmark drives
  (our turn model: `run_agent` loops `send()`); or over A2A between two agents.

## Claude Code adapter: decisions

| # | Decision | Why |
|---|---|---|
| 1 | Runs **inside** the trial's world; its tool calls never pass through our exec | It executes its own commands; it can't be driven from the host like mini |
| 2 | `Agent.prepare_image(task_image) -> image`, called by the runner right before starting the world. Claude Code returns a **derived image**, tag `harness-claude-code:<sha256(task image ID + version + Dockerfile)[:12]>`, built once (lock across worker threads) and reused | Benchmarks still name their task image; trials don't reinstall; a rebuilt task image gets a new derived image. Graders still start from `grader_image(task)` |
| 3 | Install: two build stages `node:22-alpine` and `node:22-bookworm-slim` each `npm install -g @anthropic-ai/claude-code@2.1.308`; the final stage (FROM the task image) copies the one binary matching its libc (bind mounts, so the other never lands in a layer); alpine also gets `apk add bash` | npm 2.x installs Claude Code's **native binary** (platform optional deps incl. `linux-*-musl`), so the world gets no Node. Verified on alpine (`harness-bash-operations`) and python:3.11-slim (`harness-python-tasks`): `claude --version` = 2.1.308. The Bash tool needs bash: with only busybox sh it fails "No suitable shell found" (`SHELL=/bin/sh` and `CLAUDE_CODE_SHELL=/bin/sh` don't help) |
| 4 | Credentials: `forward_env = ("ANTHROPIC_API_KEY",)` read on the host, passed per command with `exec(env=...)`; never in an image | `security.md` (b). `docker exec -e NAME` without a value (docker reads it from its own environment), so the key never appears on the host's command line; the trace records names only (`{"ANTHROPIC_API_KEY": "***"}`) |
| 5 | A turn = `claude -p --output-format stream-json --verbose --model <id without anthropic/> --dangerously-skip-permissions --max-turns <steps left> --max-budget-usd <budget left> --strict-mcp-config [--mcp-config /tmp/claude-code-mcp.json] [--resume <session>] -- <message>`, env `IS_SANDBOX=1`, `DISABLE_AUTOUPDATER=1` | Verified: as root, `--dangerously-skip-permissions` refuses ("cannot be used with root/sudo privileges") unless `IS_SANDBOX=1`. `--max-turns` works though `--help` hides it. `--max-budget-usd` counts per invocation (a resumed run with budget below the session's total still ran). `--` keeps a message starting with `-` from being read as a flag |
| 6 | MCP: the task's servers are written into Claude Code's `--mcp-config` (`{"mcpServers": {name: {"type": "http", "url": ...}}}`); Claude Code discovers the tools (`mcp__<server>__<tool>`); nothing is added to the prompt | Same as Harbor |
| 7 | Multi-turn: `--resume <session_id>` from the previous result event | Verified: same session id, history kept |
| 8 | Limits: exec timeout = what is left of the trial; on timeout `kill_agent_processes()` (killing `docker exec` leaves claude running inside), exit status `TimeLimit`, reply `""`. Steps (`model_calls` since `database.md` #20) = sum of `num_turns`; cost = the last result's `total_cost_usd` | Verified: `total_cost_usd` (and `modelUsage`) is the session's running total after `--resume`; `num_turns` is per run |
| 9 | Errors (from the result event's `terminal_reason: api_error` + the failed message's `error`): `prompt_too_long` / `refusal` (and `stop_reason: refusal`) → `AgentLimitation`; `rate_limit` / `server_error` / `overloaded` or HTTP 408/429/5xx/529 → `TransientError`; everything else (`authentication_failed`, `billing_error`, `model_not_found`, `invalid_request`, no result event) → `ClaudeCodeError` (`infra_error`, not retried) | As for mini (`errors.md`). Verified shapes with a bad key (401) and an unknown model (404) |
| 10 | Trace: the raw stream is saved as `trajectory.jsonl`; tool calls / results become `tool_call` / `tool_result` events (output capped like commands) through `AgentSession.new_events()`, which the recording session writes after each turn (also when the agent raised) | Commands Claude Code runs bypass our exec, so this is our trace for it. The stream goes to a file in the world (`/tmp/claude-code-turn-<n>.jsonl`), not exec's 1 MB-capped stdout, so a long run keeps its result and a run killed at the deadline keeps what it did |
| 11 | Reply = `Message("agent", (Text(result text),))`; exit status = Claude Code's own `subtype` (`success`, `error_max_turns`, `error_max_budget_usd`, ...) or `TimeLimit` / `LimitsExceeded` | The harness adds nothing |
| 12 | Model (2026-10-30): the harness-wide `HARNESS_MODEL` / `--model`, litellm-style `anthropic/claude-haiku-5-5` in `runs.model`; `ClaudeCode.native_model()` drops `anthropic/` for its `--model`; preflight: an Anthropic model + `ANTHROPIC_API_KEY` set | Both agents run the same model by default and record it the same way; no agent reads another agent's settings (was `MSWEA_MODEL_NAME`, mini's own) |

### Fidelity cost (agent tooling; standard in Harbor / Inspect installed agents)

- The agent's world gains `/usr/local/bin/claude` (~280 MB native binary) and, on alpine, **bash**. A bash-only
  benchmark's world is therefore not exactly the benchmark's image while Claude Code runs (no python is added;
  graders still run in their own sidecar).
- The API key is inside the world (environment of the claude process): the agent could read or leak it. Inherent to
  in-world agents; redaction keeps it out of our logs.
- Claude Code brings its own tool set (incl. WebFetch / WebSearch: the world has network). Part of "the agent".

### Live results (2026-10-29, Haiku 5.5)

| Run | Accuracy | Cost | Notes |
|---|---|---|---|
| bash-operations | 0.80 (4/5) | $0.0049 | bash-003: `wc -l` on a file without a final newline (counted 4, expected 5): the agent's miss |
| python-tasks | 1.00 (3/3) | $0.0056 | |
| MCP fixture (live test) | pass | $0.0013 | called `mcp__fixture__secret_word` on the sidecar |

### Open questions

- ~~Model setting: reuse mini's `MSWEA_MODEL_NAME`, or Claude Code's own `ANTHROPIC_MODEL`?~~ Neither (2026-10-30):
  one harness-wide `HARNESS_MODEL`, translated per agent (#12).
- Offline benchmarks: Claude Code's web tools are moot once the world has no internet (`environment.md` → Network).
- Turn off Claude Code's web tools (`--disallowed-tools WebFetch,WebSearch`) for benchmarks meant to be offline?
- τ²-style grading needs the MCP server's state: give `grade()` the service sidecars too?
- ~~Derived images (~0.4–0.7 GB each) are never removed; a `harness cleanup` could prune them.~~ Built (Phase 6):
  `harness cleanup --images` removes `harness-*` images (task and derived) no unfinished trial needs.
