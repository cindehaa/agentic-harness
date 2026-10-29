# Benchmarks

Two small starter benchmarks ship with this repo. The others are popular upstream agentic
benchmarks vendored **unmodified** (git submodules pinned to the upstream default-branch commit
as of 2026-10-29) so they are on hand to run later. No harness adapters exist for them yet.

These submodules are **opt-in** (`update = none` in `.gitmodules`), so `make init` only fetches
`agents/mini-swe-agent`. Fetch the extra benchmarks on demand:

```bash
make init-extra-benchmarks                                # all four (~1.2GB checked out, mostly tau2-bench)
make init-extra-benchmarks BENCH=benchmarks/tau2-bench   # just one
```

| Benchmark | Path | Upstream | Pinned commit | Input shape | How to get data / run upstream | Notes |
|---|---|---|---|---|---|---|
| bash-operations (starter) | `benchmarks/bash-operations` | (this repo) | n/a | plain string | Run through this harness | Tiny, local |
| python-tasks (starter) | `benchmarks/python-tasks` | (this repo) | n/a | plain string | Run through this harness | Tiny, local |
| Terminal-Bench | `benchmarks/terminal-bench` | [harbor-framework/terminal-bench-1](https://github.com/harbor-framework/terminal-bench-1) (formerly `laude-institute/terminal-bench`, redirects) | `d28711d0da26` | environment-as-input (instruction + a Docker container to act in) | Tasks are in-repo under `original-tasks/` (256 tasks). `uv tool install terminal-bench`, then `tb run --agent terminus --model <model> --dataset-name terminal-bench-core --dataset-version 0.1.1` | ~146MB git objects. Needs Docker; each task builds its own image |
| SWE-bench (harness) | `benchmarks/swe-bench` | [SWE-bench/SWE-bench](https://github.com/SWE-bench/SWE-bench) | `02e7a74ffd0b` | environment-as-input (issue text + a repo checkout at `base_commit`) | `pip install -e benchmarks/swe-bench`, then `swebench eval <dataset> -p <predictions> --run-id <id> -j <workers>` (dataset may be a local path, alias, or `SWE-bench/SWE-bench_Lite`) | ~81MB git objects. Needs Docker and lots of disk (per-instance images, roughly 100GB+ for a full run). On ARM Macs use `--task-repo` for local Buildx builds |
| SWE-bench Lite (data) | `benchmarks/swe-bench-lite/dataset.jsonl` | [princeton-nlp/SWE-bench_Lite](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite) | HF rev `6ec7bb89b934` | environment-as-input (`problem_statement` + `repo`@`base_commit`) | Already downloaded: `test` split, 300 rows. Re-fetch: `python -c "from datasets import load_dataset; load_dataset('princeton-nlp/SWE-bench_Lite', split='test').to_json('benchmarks/swe-bench-lite/dataset.jsonl', lines=True)"` | 3.6MB. Graded with the SWE-bench harness above |
| τ²-bench | `benchmarks/tau2-bench` | [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) | `7aad81c37a3a` | dynamic simulated user (LLM user simulator + tool-calling domain env) | Domains/tasks are in-repo under `data/tau2/domains/` (airline, retail, telecom, ...). `cd benchmarks/tau2-bench && uv sync`, then `tau2 run --benchmark text --agent-llm <model> ...` | **Shallow submodule** (`shallow = true`, depth 1). ~83MB packed but ~950MB checked out (`data/tau2/results/` alone is ~576MB of published runs). Needs LLM API keys for both agent and user simulator; voice track needs `--extra voice` |
| MiniWoB++ | `benchmarks/miniwob-plusplus` | [Farama-Foundation/miniwob-plusplus](https://github.com/Farama-Foundation/miniwob-plusplus) | `33c3b4ddef8c` | text instruction + environment-as-input (browser page) | Tasks are self-contained HTML pages under `miniwob/html/miniwob/` (~130 tasks). `pip install miniwob`, then `gymnasium.make('miniwob/click-test-2-v1')` (Gymnasium API) | ~11MB. Needs a browser capability: Chrome/Chromium + ChromeDriver (Selenium). No hosted site stack |
| GAIA | not vendored | [gaia-benchmark/GAIA](https://huggingface.co/datasets/gaia-benchmark/GAIA) | n/a | multimodal (question text + optional attached file: PDF, image, spreadsheet, audio) | **Gated**: accept the terms on the HF page while logged in, `huggingface-cli login`, then `huggingface-cli download gaia-benchmark/GAIA --repo-type dataset --local-dir benchmarks/gaia` (validation split has answers; test answers are hidden, scored via the leaderboard) | Not downloaded because it requires accepting terms. Also needs web browsing + file tools to solve |

Heavier web alternatives intentionally not added: WebArena / VisualWebArena (they need
self-hosted site stacks with ~100GB+ of Docker images).
