#!/usr/bin/env bash
# Coordinator loop on the lead PC (ryzen9): assign departments to idle workers.
#
#   export E14_FLEET_WORKERS=ryzen9-1,legion-1
#   export E14_FLEET_COORDINATOR=ryzen9-1
#   nohup bash scripts/fleet_scheduler.sh >> logs/fleet_scheduler.log 2>&1 & disown
set -u
cd "$(dirname "$0")/.."

PY="${E14_CROP_PY:-.venv/bin/e14detector}"
OUTPUT="${E14_CROP_OUTPUT_DIR:-data/detector_national}"
INTERVAL="${E14_FLEET_SCHEDULE_INTERVAL:-120}"
WORKERS="${E14_FLEET_WORKERS:-}"
COORD="${E14_FLEET_COORDINATOR:-}"

if [[ -z "$WORKERS" ]]; then
  echo "Set E14_FLEET_WORKERS=ryzen9-1,legion-1" >&2
  exit 1
fi

ARGS=(--output-dir "$OUTPUT" --workers "$WORKERS")
[[ -n "$COORD" ]] && ARGS+=(--coordinator "$COORD")

if [[ ! -f "$OUTPUT/fleet/queue.json" ]]; then
  echo "[fleet-scheduler] $(date -Is) initializing queue"
  "$PY" fleet-init --output-dir "$OUTPUT" --workers "$WORKERS"
fi

echo "[fleet-scheduler] $(date -Is) workers=$WORKERS interval=${INTERVAL}s"
while true; do
  echo "[fleet-scheduler] $(date -Is) schedule"
  "$PY" fleet-schedule "${ARGS[@]}" >> logs/fleet_scheduler.log 2>&1 || true
  sleep "$INTERVAL"
done
