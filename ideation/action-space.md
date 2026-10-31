# How agents act on the world (action space), MCP, and agents over ACP

**Status:** Decided 2026-10-29 (locked by Cindy); built 2026-10-29: `AgentCapabilities`, MCP sidecars,
`exec(env=, user=)`, a dedicated Claude Code adapter. **2026-10-30: the dedicated adapter is replaced by generic
ACP support** (`AcpAgent`, `agents/acp.py`; Claude Code is one ACP agent definition). Not built: τ²-bench, Python
function-calling agents, GUI actions.

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
- **ACP agents** declare theirs in their definition (`capabilities`; multi-turn always). Claude Code:
  `content_types={"text"}` (images later), `multi_turn=True`, `mcp=True`; the agent's `initialize` answer must agree.

## Still open industry-wide (talking points)

- **GUI / desktop**: no standard action protocol. Each harness ships its own (screenshots + coordinates, accessibility
  trees, VNC); an MCP computer-use server is possible but nothing is settled.
- **Uniform trace for black-box agents**: intercept model traffic (a proxy between the agent and the model API, as
  Inspect's `sandbox_agent_bridge` does). Every agent's trace then has the same shape (requests, responses, tool
  calls), whatever it logs itself. Today: our own `exec` trace for mini, ACP's `session/update` stream for in-world agents (one shape for every ACP
  agent, but only as detailed as the agent reports; cost only if it reports one).
- **Simulated users** (τ-bench style), three ways: as a tool the agent calls; as a second agent the benchmark drives
  (our turn model: `run_agent` loops `send()`); or over A2A between two agents.

## Agents over ACP: decisions (2026-10-30, replaces the dedicated Claude Code adapter)

Cindy, 2026-10-30: "scratch our adapter and just support the protocol." Every in-world agent is integrated through
the **Agent Client Protocol** ([ACP](https://agentclientprotocol.com), Zed; JSON-RPC 2.0 over the agent's
stdin/stdout, one JSON message per line). One generic adapter (`AcpAgent`, `agents/acp.py` + the JSON-RPC client
`agents/acp_client.py`) runs any ACP agent; an agent is **data** (an `AcpDefinition`), so a new or private agent
needs no code in this repo (the brief's "forking repos to protect proprietary agent implementations").
mini-swe-agent is unchanged (the provided Python agent, on the host; not an ACP agent).

### Research (2026-10-30)

- **Spec** ([overview](https://agentclientprotocol.com/protocol/overview), [transports](https://agentclientprotocol.com/protocol/transports)):
  `initialize` (protocol version `1`, client/agent capabilities) → `session/new {cwd (absolute), mcpServers}` →
  `session/prompt {sessionId, prompt: ContentBlock[]}` → the agent streams `session/update` notifications
  (`agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`, `plan`, `usage_update`, ...) and may
  send `session/request_permission`, `fs/read_text_file` / `fs/write_text_file`, `terminal/*` (only if the client
  advertised them) → the prompt's response `{stopReason: end_turn | max_tokens | max_turn_requests | refusal |
  cancelled}`. `session/cancel` is a notification; the agent then ends the turn with `cancelled`.
  [MCP servers](https://agentclientprotocol.com/protocol/session-setup): stdio always; `http` / `sse` only if the agent
  sets `mcpCapabilities.http` / `.sse`. Cost: `usage_update.cost {amount, currency}` = the session's cumulative cost
  (optional); per-turn tokens: `PromptResponse.usage` (marked UNSTABLE in the schema).
- **Who speaks it** ([agents list](https://agentclientprotocol.com/overview/agents), 61 entries;
  [registry](https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json), 44): Claude Code through
  `@agentclientprotocol/claude-agent-acp` (renamed from `@zed-industries/claude-code-acp`, now deprecated on npm;
  built on the Claude Agent SDK), Codex through `@agentclientprotocol/codex-acp`, Gemini CLI (`gemini --acp`, was
  `--experimental-acp`), GitHub Copilot CLI (`copilot --acp`), goose (`goose acp`), OpenCode (`opencode acp`),
  Cursor, Kiro, Cline, Junie, Qwen Code, Mistral Vibe, OpenHands, ... The registry's own entry format
  (`distribution`: `npx` package + args, or per-platform `binary` archive + cmd + args) is the same idea as our
  definitions.
- **Claude Code's ACP adapter** (read from `claude-agent-acp@0.99.1`'s source):
  - **Own tools, not the client's.** Its sessions use the SDK's `claude_code` tool preset (Bash, Read, Edit, Write,
    ...); it never calls `fs/*` or `terminal/*` for its work. So we advertise no fs / terminal capabilities.
  - Tool permission: in the default mode every tool call asks `session/request_permission` (options `allow_once`,
    `allow_always`, `reject_once`, ...). Bypass mode exists but refuses root unless `IS_SANDBOX` is set.
  - Multi-turn: many `session/prompt` on one session; also `session/load` / `resume`.
  - MCP: `mcpCapabilities {http, sse}`: our HTTP sidecars go straight into `session/new`.
  - Model: `ANTHROPIC_MODEL` (verified: the turns ran `claude-haiku-5-5`, `usage_update._meta["_claude/model"]`); also
    a `model` session config option (aliases such as `haiku`).
  - **Usage: yes.** A `usage_update` with `cost.amount` = Claude Code's `total_cost_usd` (session total) after each
    model loop, and `PromptResponse.usage {inputTokens, outputTokens, cachedReadTokens, cachedWriteTokens}` per
    turn. No count of model calls.
  - Install: a Node program (~470 MB with Node and the SDK's native Claude Code binary for its libc).
- **Python library:** the official [`agent-client-protocol`](https://pypi.org/project/agent-client-protocol/) (0.12,
  asyncio + pydantic). Not used: our sessions are synchronous (`send()` blocks), the client side we need is six
  methods, and a ~140-line client is easier to review line by line than an async SDK in a thread.

| # | Decision | Why |
|---|---|---|
| 1 | **An agent is data**: `AcpDefinition(version, install, install_dir, world_setup, command, forward_env, env, model_env, model_prefix, capabilities)`. Built-ins in `agents/acp_agents.toml` (`claude-code`); a private agent is a file with the same keys: `--agent path/to/agent.toml` (`runs.agent` keeps the absolute path, so a worker finds it again) | No per-agent code; a vendor ships a definition (and an image), not a fork of our repo. TOML, not JSON: the definitions need comments (why bash, why libstdc++) |
| 2 | **Install = build stages by libc**: `install.musl` / `install.glibc` / `install.any` = `{from: image, run: shell}` leaving a self-contained install in `install_dir`; the derived image (`harness-acp-<name>:<sha256(task image ID + recipe)[:12]>`, built once, cached, lock across threads) copies the stage matching the task image's libc through bind mounts (the others never land in a layer), then runs `world_setup`. A private agent: `install.any = {from = "vendor/agent:1"}` (its install copied out of its own image) | The old Claude Code recipe, generalised. claude-code: `npm install -g --prefix /opt/acp-agent @agentclientprotocol/claude-agent-acp@0.99.1` + the stage's `node`, in `node:22-alpine` / `node:22-bookworm-slim`; `world_setup` adds `bash` (the Bash tool needs it) and `libstdc++` (Node needs it) on alpine (Debian images have libstdc++: apt depends on it) |
| 3 | **Runs inside the world**, launched with `Environment.start_process(argv, env)` (Docker: `docker exec -i -w <workdir> -e NAME ...`); the harness speaks ACP over its stdin/stdout; stderr goes to `agent-stderr.log` | As Claude Code before: the agent executes its own tools in the world. One process per trial, alive across turns |
| 4 | **Credentials**: `forward_env` (claude-code: `ANTHROPIC_API_KEY`), read on the host, passed with `docker exec -e NAME` (value from docker's own environment, never in argv or an image); added to redaction; the trace records names only | `security.md` (b), unchanged |
| 5 | **Setup**: `initialize` (version 1; client capabilities: no fs, no terminal) → `session/new {cwd: the world's workdir (Task.workdir), mcpServers: the task's sidecars as {type: http, name, url, headers: []}}`. Startup timeout 120 s. A definition that says `mcp` for an agent without `mcpCapabilities.http`, or another protocol version → `AcpAgentError` | The runner still checks capabilities once, before the world starts, from the definition; the agent's own answer must agree or the definition is wrong |
| 6 | **Turns** = `session/prompt` on the one session; reply = the agent's message chunks since its last tool call (its final message); exit status = the `stopReason`, or ours (`TimeLimit`, `LimitsExceeded`) | Multi-turn for free (process + session stay up). Earlier text is in the trace (`agent_text`) |
| 7 | **Permission requests: allow, once**, inside the world (`allow_once`, else `allow_always`, else cancelled); each one traced (`permission`). Never on the host: the agent only runs in the world | The container is the boundary. Not "always": an always-rule may be written into the workspace (Claude Code's `.claude/settings.local.json`) and graded with it. Generic (no per-agent bypass flags or `IS_SANDBOX`) |
| 8 | **Limits**: deadline → `session/cancel`, 15 s grace for the agent to end the turn, then kill (the `docker exec` client and, since that leaves the agent running inside, `kill_agent_processes()`); budget → `session/cancel` once `usage_update`'s cost passes `cost_usd`. Steps (`max_steps`): **not enforced** (ACP reports no model calls; the old `--max-turns` was Claude Code's own flag). **2026-10-31:** enforced where the agent keeps it itself, through the definition's `limits_meta` (claude-code: `_meta.claudeCode.options.maxTurns`, per prompt); otherwise recorded `"enforced": false` (`agents-and-runner.md` L7) | Generic ACP has no limits in `session/new`; the deadline and the budget are what the harness can see |
| 9 | **Trace**: `session/update` → events: `tool_call` (id, name, title, kind, input = `rawInput`) + `tool_result` (status, output = `rawOutput` else the displayed content, capped like commands) once the call completes or fails (a call's input often arrives in a later update); `agent_text`, `thought`, `plan`, `permission`, `session` (agent name + version, session id, the model config option); the full protocol both ways in `acp.jsonl` | Agent-neutral: any ACP agent's trace has the same shape |
| 10 | **Cost / tokens**: cost = the last `usage_update.cost.amount` (the session's total); tokens = each turn's `PromptResponse.usage` (cached counted as input, as litellm does); an agent that reports tokens but no cost is priced with litellm from `runs.model`; one that reports neither: cost 0 = unknown. `model_calls` = 0 for ACP agents | Use what the protocol carries, never fake it. The general fix for black-box agents is a model-traffic interception proxy (talking point below) |
| 11 | **Errors**: a JSON-RPC error from `session/prompt` has only a message: "overloaded" / "rate limit" / 5xx words → `TransientError` (retried); "prompt is too long" / context window → `AgentLimitation`; anything else → `AcpAgentError` (`infra_error`, not retried). `stopReason: refusal` → `AgentLimitation`. The agent's process ending: the world gone → `EnvironmentLost` (the runner's container-death rules), else `AcpAgentError` with its stderr's tail | ACP has no error kinds for provider failures; message text is all there is |
| 12 | **Model**: `model_env` (claude-code: `ANTHROPIC_MODEL`) set to the run's model without `model_prefix` (`anthropic/`); preflight: a model of that prefix + `forward_env` set | One harness-wide model (`HARNESS_MODEL` / `--model`), translated by data instead of `native_model()` code |

### What changed for Claude Code (vs the dedicated adapter, 2026-10-29)

- Same: in-world, cached derived image, API key per process, HTTP MCP sidecars, multi-turn, cost from Claude Code
  itself, trace of tool calls / results.
- Better: no per-agent code; the trace also has the agent's narration, thoughts and permissions; the agent no longer
  restarts per turn (one process, one session).
- Worse: the step limit (`--max-turns`) is gone (deadline + budget remain; back for Claude Code on 2026-10-31 through
  `limits_meta`, per prompt); `model_calls` is 0; a turn killed at
  the deadline after the agent ignored the cancel loses that turn's cost (Claude Code reports it only at its loop's
  end; before: priced from the stream); the image is bigger (~470 MB: Node + the adapter + Claude Code, was ~280 MB).
- Version: Claude Code is now whatever `@anthropic-ai/claude-agent-sdk` the pinned adapter depends on (0.99.1 →
  SDK 0.3.312), pinned through the adapter's version.

### Fidelity cost (agent tooling; standard in Harbor / Inspect installed agents)

- The agent's world gains its install (`/opt/acp-agent`, ~470 MB for claude-code: Node, the adapter, Claude Code's
  native binary) and, on alpine, **bash** and **libstdc++**. A bash-only benchmark's world is therefore not exactly
  the benchmark's image while an in-world agent runs (no python is added; graders still run in their own sidecar).
- The API key is inside the world (environment of the agent's process): the agent could read or leak it. Inherent
  to in-world agents; redaction keeps it out of our logs.
- Claude Code brings its own tool set (incl. WebFetch / WebSearch: the world has network). Part of "the agent".

### Live results (Haiku 5.5)

| Run | Dedicated adapter (2026-10-29) | Through ACP (2026-10-31) | Notes |
|---|---|---|---|
| bash-operations | 0.80 (4/5), $0.0049 | 0.80 (4/5), $0.0066 | Both: bash-003, `wc -l` on a file without a final newline (counted 4, expected 5): the agent's miss. ~35k input tokens per task (mostly cached prompt) |
| python-tasks | 1.00 (3/3), $0.0056 | 1.00 (3/3), $0.0055 | |
| MCP fixture (live test) | pass, $0.0013 | pass, $0.0014–0.0030 | ACP: Claude Code first loads the deferred tool (`ToolSearch`), then calls `mcp__fixture__secret_word` |
| Two turns (live test) | (resume verified by hand) | pass, $0.0015 | one process, one session: turn 2 recalled turn 1 |

Cost differences are within run-to-run noise plus the first trial's prompt-cache write; the Claude Code version
also moved (2.1.308 → the SDK's, 0.3.312).

### Open questions

- Offline benchmarks: Claude Code's web tools are moot once the world has no internet (`environment.md` → Network).
- Turn off Claude Code's web tools for benchmarks meant to be offline? Over ACP that is agent-specific data
  (`_meta.claudeCode.options.disallowedTools` in `session/new`): a definition field for `session/new` `_meta` if needed.
- Step limit for ACP agents: none in the protocol. A definition could carry the agent's own option (Claude Code:
  `_meta.claudeCode.options.maxTurns`), or the harness could count model calls through an interception proxy.
- Uniform cost for agents that report none: a model-traffic interception proxy (the agent's base URL pointed at a
  harness proxy that logs and prices every call), also the uniform trace above.
- τ²-style grading needs the MCP server's state: give `grade()` the service sidecars too?
- ~~Derived images (~0.4–0.9 GB each) are never removed; a `harness cleanup` could prune them.~~ Built (Phase 6):
  `harness cleanup --images` removes `harness-*` images (task and derived, `harness-acp-*`) no unfinished trial needs.
