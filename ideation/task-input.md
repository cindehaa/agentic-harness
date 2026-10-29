# What exactly is a task input?

**Status:** Adopted 2026-10-29. Every trial is a series of **turns driven by
the benchmark**. What the agent learns arrives through two channels: the
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

| | A. Prompt-centric (Inspect) | B. Env-centric (Harbor, METR) | C. Pass-through (HAL) | **D. Benchmark-driven turns (AgentBeats "episode", τ)** | E. Gym loop (OSWorld, BrowserGym) |
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

## Decision: benchmark-driven turns are the backbone (D)

Every trial's turns are driven by the benchmark through `run_agent`.
Single-shot is just a one-turn trial. Agents stay **in-process Python
adapters** behind a blocking `send()`. Unlike AgentBeats or A2A, they are
**not** network services. The final names are under "Locked interface" below.

### The loop, state by state

1. **Start the world.** The runner starts the task's image; per-task world
   setup (files, DBs) is already baked into it (see "World setup" below).
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

Limits apply at **three levels**: per turn, per attempt (all turns), and a
maximum number of turns. Without them the turns can deadlock, with the agent waiting for
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
| Grading | Can also happen between turns (per-step checks). |
| Reproducibility | Worse when the benchmark uses an LLM user, so we record the full transcript and seeds. |
| Queue (Part 2) | No change: the turns run inside the trial job, still one job per trial. |

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
- A trial's turns are sequential. That's fine: parallelism comes from running many trials.

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

## Locked interface (2026-10-29)

```python
class Benchmark(ABC, Generic[TaskT]):
    def tasks(self) -> list[TaskT]: ...
    def setup(self, tasks: list[TaskT]) -> None: ...  # optional, once per run (build images, download data)
    def run_agent(self, task: TaskT, agent: AgentSession, env: Environment) -> None: ...  # one or more turns
    def grade(self, task: TaskT, env: Environment, transcript: Transcript) -> Score: ...
```

A simple benchmark is a **one-turn trial**:

```python
def run_agent(self, task, agent, env):
    agent.send(Message("user", (Text(task.problem_statement),)))
```

A complex one (τ-bench) loops: `reply = agent.send(msg); msg = user.respond(reply)`
until the simulated user stops.

- **`AgentSession`** is the harness-wrapped session. It has a single blocking
  `send(message) -> Message` (send and wait are merged). It logs every turn,
  builds the transcript, enforces limits including the maximum number of
  turns, and raises `Unsupported` when a message has content the agent can't
  accept. The benchmark never returns the transcript; the harness has it.
- **`grade(task, env, transcript)`** grades the world state (our two,
  SWE-bench), the agent's answer (GAIA, τ-bench outputs), or both. Grader prep
  (installing deps, restoring tests) is the first lines of `grade()`. The
  harness kills leftover agent processes before calling it.
- **Messages carry typed `Content`:** `Text`, or `File` (path in the world,
  media type). Prior context is earlier messages. Files are always placed in
  the container.
- **World = `Environment` exposing capabilities.** Today: shell + files.
  Later: browser, tools (MCP), and a simulated-user channel. A mismatch between
  what the benchmark needs and what the agent can use makes the trial
  `unsupported`.

### World setup: baked into the image (D, 2026-10-29)

Per-trial world setup lives in the task's image, so a trial only starts it.
`Task.image` names it; the optional `setup(tasks)` builds missing images once
per run (default no-op; most benchmarks already ship images). Example
multi-stage Dockerfile: stage 1 `python:3.11-slim` runs the benchmark's own
`setup.py` into `/out`; stage 2 `FROM alpine:latest` +
`COPY --from=setup /out /workspace`, so the agent's world never gains python.
**Adapters reuse the benchmark's own setup and grader scripts** (fidelity).

Rejected: two hooks (`world_image` + `build_world`); `WorldSpec` (over-engineered —
most benchmarks already provide an image); reimplementing setup in the
adapter; setup on the host + upload; install python, run setup, uninstall;
a setup sidecar container.

### Terminology

Run (one `harness run`; Harbor calls it a job) → **Trial** (one task × agent;
the queue unit; ends with one `Outcome`) → **Attempt** (a retry after an
infra error) → **Turn** (one `send` → reply).

We dropped "Episode" (jargon); a trial's back-and-forth is just its turns.

### Types

`types.py` holds `Task` (`benchmark`, `id`, `image`; adapters subclass it),
`Content` (`Text | File`), `Message` (role `"user" | "agent"`, `content`),
`Transcript` and `Score`/`Value`. `ExecResult` lives in
`environments/base.py`. `Outcome` (statuses `graded`, `grader_error`, `error`,
`unsupported`) and `Limits` are deferred to Phase 3.

- **`Value = Any`**, but it must be JSON-serializable. The recorder checks;
  a non-serializable value makes the outcome `grader_error`.
- **`Score.metadata`** is non-verdict grader info for humans and debugging
  (stdout/stderr, expected vs actual, lists of passed/failed tests). Stored as
  JSON, never aggregated.
- `File.inline` removed (2026-10-29) — added complexity; re-add if a benchmark needs the agent to perceive a file directly (e.g. VisualWebArena query images).
- **Transcript vs Trace** are separate. The Transcript is the conversation
  (benchmark ↔ agent messages), read by graders, defined in `types.py`. The
  Trace is the agent's internal actions (commands, tool calls): recorder events
  plus raw trajectory files, built in Phase 4.

### Rejected along the way

- **A separate `opening()`/`load_input()` method.** Collapsed into
  `run_agent`: a simple task is just one turn.
- **A generator-based turn loop** (`reply = yield msg`). It keeps the agent out
  of benchmark code, but it's an unusual idiom and harder to explain.
- **Separate `send()` + `wait_until_done()`.** Merged into one blocking `send`.
- **A separate `prepare_grading()`.** Folded into `grade()`.
- **The names `run_episode` ("episode" is RL jargon), `interact` and `run`.** `run_agent` matches the
  trial phases and avoids clashing with Run.
- **Earlier names:** `list_tasks` → `tasks`, per-trial `setup` → `build_world`
  → removed (baked into images; `setup(tasks)` is now the once-per-run hook),
  `Part` → `Content`, `TrialOutcome` → `Outcome`.

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

### Parity check: future command, out of scope

Decided (2026-10-29) as a future standalone command:
`harness parity --benchmark X --agent Y`. It follows Harbor's method:

- run the same agent and model on the benchmark's original harness and on ours;
- do at least 2–3 runs per side and compare mean ± SEM;
- treat the adapter as faithful only if the score ranges overlap.

It is **out of scope for the take-home**. It is a design note and an onsite
talking point only.

## Scope

Phase 1 implements only text messages, a shell + files container, and
one-turn `run_agent`. Multimodal parts, browsers, tools, simulated
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
