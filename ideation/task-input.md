# What exactly is a task input?

**Status:** Under discussion — two-channel model proposed, not yet adopted (2026-10-29)

We kept running into this while designing `Task`: "the instruction" is only
the simplest case. Across agentic benchmarks, the input sits on a spectrum.

## The spectrum

1. **A plain string.** Our two benchmarks, SWE-bench (`problem_statement`),
   Terminal-Bench / Harbor (`instruction.md`), METR (`get_instructions()`).
2. **A combination of modalities.** Text plus images, audio, video or files:
   GAIA (question + one attached xlsx/pdf/png/mp3), VisualWebArena (inline
   query images), SWE-bench Multimodal (issue screenshots).
3. **The environment itself is the input.** WebArena (live sites + saved
   login cookies), AppWorld (app databases behind an API), OSWorld (VM set up
   by download/open/launch steps).
4. **Dynamic input that arrives during the run.** τ-bench / τ²-bench: a
   simulated user reveals the task by talking; τ² can also start from a prior
   `message_history`; the A2A protocol pauses a task with `INPUT_REQUIRED`.

## Survey: what the agent gets, by channel

| Benchmark | Opening (told at start) | World (acts on) |
|---|---|---|
| Ours, SWE-bench, Terminal-Bench | Instruction text | Container with files / repo |
| GAIA | Question | Container + attached file |
| VisualWebArena | Instruction + inline images | Browser + websites |
| WebArena | Instruction | Browser, websites, saved login |
| AppWorld | Instruction | REPL with app APIs + DBs |
| τ²-bench | Prior conversation (`message_history`) | Tools, DBs, simulated user |
| τ-bench | Policy text | Tools + simulated user (gives the task) |

## Model under consideration: two channels

Everything the agent learns arrives through one of two channels, both
provided by the benchmark adapter:

```python
class Benchmark(ABC, Generic[TaskT]):
    def load_input(self, task: TaskT) -> list[Message]: ...    # 1. opening
    def setup(self, task: TaskT, env: Environment) -> None: ... # 2. world
```

1. **Opening = messages with parts.** Parts are text, or a file
   (`env_path`, `media_type`, `inline` flag). Prior context = earlier messages
   in the list. Files are always placed in the container; `inline` ones are
   also put in the prompt for agents that accept them.
2. **World = the `Environment`.** Today: a container you can exec in. Later it
   can also expose a browser or a simulated-user channel, so τ-bench fits
   without changing `Task`.

## Rejected

- **Chat format as the whole input.** It's the model-eval paradigm (input goes
  straight to a model). Agents own their system prompt and loop; benchmark
  `system`/`assistant` messages would clash with them. Messages are kept only
  for the opening, where a conversation really does start.
- **`instruction: str` only.** Too narrow: can't express attachments, prior
  context, or inputs that live in the environment.

## Fidelity

Changing how input is delivered can change scores, and no harness passes the
benchmark's native input through untouched:

- **inspect_evals:** GAIA files copied to `/shared_files/` plus an injected
  path sentence; SWE-bench wrapped as "Please solve the following coding
  issue"; no score comparison with the originals.
- **OpenHands:** heavy Jinja templating; GAIA images inlined, other files
  uploaded to `/workspace/`.
- **HAL:** passes the native record through; the agent builds its own prompt.
- **Harbor:** the only one with a fidelity test. An adapter is accepted only
  after a **parity experiment**: same agent and model on the original harness
  vs Harbor, ≥2–3 runs per side, mean ± SEM, score ranges must overlap, and
  any prompt change must be mirrored on both sides.

Our rules:

1. Typed `Task` subclasses keep the benchmark's native record.
2. The exact rendered opening is logged for every attempt.
3. Parity runs are the acceptance test for an adapter (extends `harness validate`).

## Scope

Phase 1 implements only text messages + a container. Multimodal parts,
browsers, simulated users and parity runs are designed-for but not built;
they go in the README as design notes.

## Sources

- [Harbor adapters docs](https://github.com/laude-institute/harbor/blob/main/docs/content/docs/datasets/adapters.mdx)
- [inspect_evals GAIA](https://github.com/UKGovernmentBEIS/inspect_evals/blob/main/src/inspect_evals/gaia/dataset.py),
  [SWE-bench](https://github.com/UKGovernmentBEIS/inspect_evals/blob/main/src/inspect_evals/swe_bench/swe_bench.py)
- [HAL harness](https://github.com/princeton-pli/hal-harness)
- [OpenHands benchmarks](https://github.com/OpenHands/benchmarks)
- [τ²-bench `tasks.py`](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/data_model/tasks.py)
- [A2A protocol](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto)
- [VisualWebArena](https://github.com/web-arena-x/visualwebarena)
- [OSWorld](https://github.com/xlang-ai/OSWorld)
