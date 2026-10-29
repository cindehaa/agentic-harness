# What exactly is a task input?

**Status:** Adopted 2026-10-29. Every trial is an **episode driven by the
benchmark**. What the agent learns arrives through two channels: the
**opening** (the messages it's sent) and the **world** (the environment it acts on).

We kept running into this while designing `Task`. "The instruction" is only
the simplest case; across agentic benchmarks, input sits on a spectrum.

## The spectrum

1. **A plain string.** Our two benchmarks, SWE-bench (`problem_statement`),
   Terminal-Bench / Harbor (`instruction.md`), METR (`get_instructions()`).
2. **A combination of modalities.** Text plus images, audio, video or files:
   GAIA (a question plus one attached xlsx/pdf/png/mp3), VisualWebArena (images
   shown inline), SWE-bench Multimodal (issue screenshots).
3. **The environment itself is the input.** WebArena (live sites plus saved
   login cookies), AppWorld (app databases behind an API), OSWorld (a VM set
   up by download/open/launch steps).
4. **Input that arrives during the run.** τ-bench / τ²-bench: a simulated
   user reveals the task by talking. τ² can also start from a prior
   `message_history`. The A2A protocol pauses a task with `INPUT_REQUIRED`.

## Survey: what the agent gets, by channel

| Benchmark | Opening (told at start) | World (acts on) |
|---|---|---|
| Ours, SWE-bench, Terminal-Bench | Instruction text | Container with files / repo |
| GAIA | Question | Container + attached file |
| VisualWebArena | Instruction + inline images | Browser + websites |
| WebArena | Instruction | Browser, websites, saved login |
| AppWorld | Instruction | REPL with app APIs + DBs |
| τ²-bench | Prior conversation (`message_history`) | Tools, DBs, simulated user |
| τ-bench | Policy text | Tools + simulated user (who gives the task) |

## Five approaches compared

| | A. Prompt-centric (Inspect) | B. Env-centric (Harbor, METR) | C. Pass-through (HAL) | **D. Episode (AgentBeats, τ)** | E. Gym loop (OSWorld, BrowserGym) |
|---|---|---|---|---|---|
| Who controls the loop | agent | agent | agent | **benchmark, between turns** | harness, every action |
| Plain text | ✓ | ✓ | ✓ | ✓ | ✓ |
| Media | inline | as files | as files | message parts | observations |
| World as input | ✗ | ✓ | ✓ | ✓ | ✓ |
| Dynamic input | ✗ fixed transcript | ✗ (Harbor adds steps) | ✗ | ✓ | ✓ |
| Works with black-box agents | partly | ✓ best | ✗ agent must know each benchmark | ✓ (as a Python session) | ✗ agent must be step-by-step |
| Fidelity | medium | medium | highest | high | high |
| Harness complexity | medium | lowest | low (agents carry it) | highest | medium |

**The key difference is how often control is handed back.** In every
approach the agent gets information, acts on its own, then hands control
back. **B** does this once. **D** does it once per turn. **E** takes control
back after every single action. A, B and C differ only in how the starting
information is packaged.

**D is B repeated, with the benchmark deciding whether to repeat.** It gives
the finest control possible over an agent that runs its own loop.

**E was rejected.** It needs agents that act one step at a time. Black-box
agents (Claude Code, mini-swe-agent) run their own think/act/observe loop and
can't be driven step by step. We'd have to know too much about each agent.

## Decision: episodes are the backbone (D)

Every trial is an episode that the benchmark drives. Single-shot is just the
default `run_episode`: send the opening, wait until the agent is done, grade.
Agents stay **in-process Python adapters** with `send()` and
`wait_until_done()`. Unlike AgentBeats or A2A, they are **not** network
services.

### The loop, state by state

1. **Build the world.** `setup()` prepares the container, files, DBs.
2. **Opening.** The benchmark sends its first message(s).
3. **Agent's turn.** The agent acts on the world as much as it likes, within
   limits. The harness logs every action.
4. **Hand back.** The agent says it's done or replies. What counts as "done"
   depends on the agent: mini-swe-agent raises `Submitted`; Claude Code's
   process exits (it continues later with `claude -p --resume <id>`).
5. **The benchmark decides.**
   - **Stop:** go to grading.
   - **Continue:** send the next step's instruction, or the simulated user's
     next message, optionally after checking intermediate progress. Back to 3.
6. **Grade.** The benchmark inspects the world and/or the transcript and
   returns a `Score`.

Limits apply at **three levels**: per turn, per episode, and a maximum number
of turns. Without them the episode can deadlock, with the agent waiting for
the user while the benchmark waits for the agent.

Most benchmarks (ours included) go through the loop once: opening, agent,
done, grade.

### Implications

| Area | Change vs B |
|---|---|
| Benchmark adapters | Can contain active logic, e.g. τ-bench's LLM-simulated user (its own model config, cost, randomness). |
| Agent adapters | Must support resuming to run multi-turn benchmarks (mini: append to history; Claude Code: `--resume`). Otherwise the trial is `unsupported` for multi-turn and only single-shot works. |
| "Done" | Defined per agent. |
| Logging | Events gain a turn index; the transcript is a list of turns. |
| Limits | Three levels instead of one. |
| Grading | Can also happen mid-episode (per-step checks). |
| Reproducibility | Worse when the benchmark uses an LLM user, so we record the full transcript and seeds. |
| Queue (Part 2) | No change: the episode runs inside the trial job, still one job per trial. |

### Pros and cons

**Pros**
- One model covers single-shot, multi-step and conversational tasks.
- Fidelity: the benchmark's own interaction logic stays in its adapter.
- Agents stay black boxes (unlike E).
- No N×M agent-benchmark integrations (unlike C).
- Every message passes through the harness, so we always have a log of
  exactly what the agent saw.

**Cons**
- More moving parts: sessions, resume, per-agent "done", three levels of limits.
- Multi-turn support is extra work in each agent adapter.
- Benchmark adapters can become programs with their own bugs, cost and randomness.
- An episode is sequential. That's fine: parallelism comes from running many trials.

### Gym-style benchmarks (OSWorld) still fit D

We expose the benchmark's actions as **tools** (e.g. MCP `screenshot`,
`click`, `type`) inside the world, and the agent runs its own loop. The
fidelity cost is that the agent, not the harness, decides when to observe the
screen, so these adapters need a parity check.

## Prompt-purity rule

- **The harness adds nothing** to what the agent sees.
- **The agent's own scaffolding** (mini-swe-agent's system and instance
  templates, Claude Code's system prompt) is part of the agent being evaluated.
- **Formats a benchmark requires** (e.g. GAIA's answer format) belong to the
  benchmark adapter, are logged with the rendered opening, and are covered by
  parity checks.

## Interface sketch

```python
class Benchmark(ABC, Generic[TaskT]):
    def list_tasks(self) -> list[TaskT]: ...
    def setup(self, task: TaskT, env: Environment) -> None: ...      # build the world
    def load_input(self, task: TaskT) -> list[Message]: ...          # the opening
    def run_episode(self, task: TaskT, agent: AgentSession, env: Environment) -> Score:
        agent.send(self.load_input(task))                            # default: single-shot
        agent.wait_until_done()
        return self.grade(task, env, agent.transcript)
    def grade(self, task: TaskT, env: Environment, transcript: Transcript) -> Score: ...
```

- **`grade(task, env, transcript)`:** some benchmarks grade the world state
  (our two, SWE-bench); others grade the agent's final answer (GAIA, τ-bench
  outputs). Both are available to `grade`.
- **Opening = messages with typed parts.** Parts are text, or a file
  (`env_path`, `media_type`, `inline` flag). Prior context is just earlier
  messages in the list. Files are always placed in the container; `inline`
  ones also go in the prompt for agents that accept them.
- **World = `Environment` exposing capabilities.** Today: shell + files.
  Later: browser, tools (MCP), and a simulated-user channel. The benchmark
  declares what it needs and the agent adapter declares what it can use; a
  mismatch makes the trial `unsupported`.
- **`AgentSession`:** `send(messages)`, `wait_until_done()`, `transcript`.

## Rejected

- **Chat format as the whole input.** That's the model-eval paradigm, where
  the input goes straight to a model. Agents own their system prompt and their
  loop, so a benchmark's `system`/`assistant` messages would clash with them.
  We use messages only for the opening, where a conversation really does start.
- **`instruction: str` only.** Too narrow: it can't express attachments, prior
  context, or inputs that live in the environment.
- **E (gym loop):** see above.

## Fidelity

Changing how input is delivered can change scores, and no harness passes the
benchmark's native input through untouched:

- **inspect_evals:**
  - GAIA: files are copied to `/shared_files/` and a sentence giving the path
    is injected.
  - SWE-bench: the issue is wrapped as "Please solve the following coding issue".
  - No score comparison with the originals.
- **OpenHands:** heavy Jinja templating. GAIA images are inlined; other files
  are uploaded to `/workspace/`.
- **HAL:** passes the native record through, and the agent builds its own prompt.
- **Harbor:** the only one with a fidelity test. An adapter is accepted only
  after a **parity experiment**:
  - same agent and model, run on the original harness and on Harbor;
  - at least 2–3 runs per side, reported as mean ± SEM;
  - the score ranges must overlap;
  - any prompt change must be mirrored on both sides.

Our rules:

1. Typed `Task` subclasses keep the benchmark's native record.
2. The exact rendered opening is logged for every attempt.
3. Parity runs are the acceptance test for an adapter (an extension of `harness validate`).

## Scope

Phase 1 implements only text messages, a shell + files container, and the
default single-shot `run_episode`. Multimodal parts, browsers, tools, simulated
users, multi-turn resume and parity runs are designed for but not built. They
go in the README as design notes.

## Sources

- [Harbor adapters docs](https://github.com/laude-institute/harbor/blob/main/docs/content/docs/datasets/adapters.mdx)
- [inspect_evals GAIA](https://github.com/UKGovernmentBEIS/inspect_evals/blob/main/src/inspect_evals/gaia/dataset.py),
  [SWE-bench](https://github.com/UKGovernmentBEIS/inspect_evals/blob/main/src/inspect_evals/swe_bench/swe_bench.py)
- [HAL harness](https://github.com/princeton-pli/hal-harness)
- [OpenHands benchmarks](https://github.com/OpenHands/benchmarks)
- [τ²-bench `tasks.py`](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/data_model/tasks.py)
- [A2A protocol](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto)
- [AgentBeats tutorial](https://github.com/RDI-Foundation/agentbeats-tutorial)
- [VisualWebArena](https://github.com/web-arena-x/visualwebarena)
- [OSWorld](https://github.com/xlang-ai/OSWorld)
