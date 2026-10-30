#!/usr/bin/env bash
# End-to-end demo of queue mode: submit two runs, work them with a background worker, kill that worker mid-run,
# restart it, and show that the lost trial is reclaimed (lease expiry -> infra_error "worker lost" -> retried).
#
# Needs Docker running, `make install`, and .env (ANTHROPIC_API_KEY, MSWEA_MODEL_NAME). Calls the model API:
# about half a cent on Claude Haiku 5.5. Takes ~3-4 minutes, mostly waiting out the 120 s lease.
# DEMO_PAUSE=1 waits for Enter between steps (for presenting).
set -euo pipefail
cd "$(dirname "$0")/.."

HARNESS=.venv/bin/harness  # the real python process, so $! below is the worker itself (not a uv wrapper)
LOG_DIR=runs/demo
mkdir -p "$LOG_DIR"

# The harness loads .env itself; loading it here too makes the variables visible to this script's checks.
if [[ ! -f .env ]]; then echo "missing .env (see README: Setup)" >&2; exit 1; fi
set -a; . ./.env; set +a

step() {
  echo
  echo "=== $*"
  if [[ "${DEMO_PAUSE:-0}" == 1 ]]; then read -r -p "(Enter to continue) " _; fi
}

# Cleanup on any exit: stop a worker still running in the background.
WORKER_PID=""
trap '[[ -n "$WORKER_PID" ]] && kill "$WORKER_PID" 2>/dev/null || true' EXIT

step "1. Submit two runs without --wait: each returns at once with a run id; nothing runs yet"
RUN1=$($HARNESS run --agent agents/mini-swe-agent --benchmark bash-operations | tee /dev/stderr \
  | sed -n 's/^Submitted run \([^:]*\):.*/\1/p')
RUN2=$($HARNESS run --agent agents/mini-swe-agent --benchmark python-tasks | tee /dev/stderr \
  | sed -n 's/^Submitted run \([^:]*\):.*/\1/p')
$HARNESS status  # both queued, with "no worker running"

step "2. Start a worker in the background: 3 threads, so up to 3 trial containers at once"
$HARNESS worker --concurrency 3 > "$LOG_DIR/worker-1.log" 2>&1 &
WORKER_PID=$!
echo "worker pid $WORKER_PID, log $LOG_DIR/worker-1.log"

step "3. Poll status until trials are running (the first worker per run builds its images first)"
for _ in $(seq 1 120); do
  $HARNESS status
  # a trial row whose STATE column is "running"
  if $HARNESS status --run_id "$RUN1" | grep -qE '^[^ ]+ +running( |$)' \
    || $HARNESS status --run_id "$RUN2" | grep -qE '^[^ ]+ +running( |$)'; then
    break
  fi
  sleep 3
done
$HARNESS status --run_id "$RUN1"

step "4. Kill the worker with SIGKILL mid-trial: no cleanup runs, its leases stay in the DB until they expire"
kill -9 "$WORKER_PID"
WORKER_PID=""
sleep 1
$HARNESS status                      # running trials are now orphaned; "no worker running"
$HARNESS status --run_id "$RUN1"

step "5. Restart the worker; it finishes pending trials now and reclaims the orphaned ones once their lease expires"
$HARNESS worker --concurrency 3 --exit-when-idle > "$LOG_DIR/worker-2.log" 2>&1 &
WORKER_PID=$!
while kill -0 "$WORKER_PID" 2>/dev/null; do
  $HARNESS status
  sleep 10
done
wait "$WORKER_PID" || true
WORKER_PID=""
grep -h "worker lost" "$LOG_DIR/worker-2.log" || echo "(no lease expired: the kill landed between trials)"

step "6. Per-trial state: a reclaimed trial shows 2 attempts (attempt 1 = infra_error, worker lost)"
$HARNESS status --run_id "$RUN1"
$HARNESS status --run_id "$RUN2"

step "7. Results for both runs"
$HARNESS results --run_id "$RUN1" --format summary
echo
$HARNESS results --run_id "$RUN2" --format summary

# The killed worker's trial containers keep running until their lifetime (3 x trial timeout + 600 s) runs out;
# there is no `harness cleanup` yet. They are labelled, so they can be listed with:
#   docker ps --filter label=agentic-harness=1
