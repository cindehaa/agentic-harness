.PHONY: init init-extra-benchmarks install bash-example py-example

EXTRA_BENCHMARKS := benchmarks/terminal-bench benchmarks/swe-bench benchmarks/tau2-bench benchmarks/miniwob-plusplus
BENCH ?= $(EXTRA_BENCHMARKS)

init:
	@echo "Initializing git submodules..."
	git submodule update --init --recursive
	@echo "Extra benchmarks are opt-in: make init-extra-benchmarks [BENCH=benchmarks/<name>]"

# --checkout overrides `update = none` in .gitmodules; tau2-bench stays shallow via its `shallow = true`.
init-extra-benchmarks:
	git submodule update --init --checkout $(BENCH)

install:
	@echo "Installing dependencies..."
	uv sync
	@echo "Dependencies installed."
	@echo "Activate the environment with: source .venv/bin/activate"

bash-example:
	bash examples/run_bash_example.sh

py-example:
	bash examples/run_python_example.sh
