#!/usr/bin/env bash
# Fleet worker: pull-db + pull-fleet → crop one assigned department → complete → repeat.
#
#   export E14_WORKER_ID=legion-1   # must match fleet queue worker id
#   export E14_FLEET_WORKERS=ryzen9-1,legion-1
#   nohup bash scripts/crop_supervisor_fleet.sh >> logs/crop_supervisor.log 2>&1 & disown
#
# Coordinator (ryzen9): run scripts/fleet_scheduler.sh in another terminal.
set -u
cd "$(dirname "$0")/.."

WORKERS="${E14_CROP_WORKERS:-32}"
INPUT="${E14_CROP_INPUT_DIR:-data/actas}"
OUTPUT="${E14_CROP_OUTPUT_DIR:-data/detector_national}"
PY="${E14_CROP_PY:-.venv/bin/e14detector}"
WORKER="${E14_WORKER_ID:-$(hostname -s)}"
PUBLISH_FLEET="${E14_FLEET_PUBLISH_ON_COMPLETE:-1}"

echo "[crop-fleet] $(date -Is) worker=$WORKER workers=$WORKERS in=$INPUT out=$OUTPUT"
while true; do
  echo "[crop-fleet] $(date -Is) sync"
  "$PY" pull-db --output-dir "$OUTPUT" >> logs/national_crop.log 2>&1 || true
  "$PY" pull-fleet --output-dir "$OUTPUT" >> logs/national_crop.log 2>&1 || true

  DEPT=$("$PY" fleet-current --output-dir "$OUTPUT" --worker "$WORKER" 2>/dev/null || true)
  if [[ -z "$DEPT" ]]; then
    echo "[crop-fleet] $(date -Is) no assignment for $WORKER — sleep 60s (is fleet_scheduler running?)"
    sleep 60
    continue
  fi

  echo "[crop-fleet] $(date -Is) cropping dept $DEPT"
  "$PY" process --input-dir "$INPUT" --output-dir "$OUTPUT" --workers "$WORKERS" --crop-only \
    --depto "$DEPT" >> logs/national_crop.log 2>&1
  code=$?

  if [[ "$PUBLISH_FLEET" == "1" ]]; then
    "$PY" fleet-complete --output-dir "$OUTPUT" --depto "$DEPT" --worker "$WORKER" \
      >> logs/national_crop.log 2>&1 || true
  else
    "$PY" fleet-complete --output-dir "$OUTPUT" --depto "$DEPT" --worker "$WORKER" --no-publish \
      >> logs/national_crop.log 2>&1 || true
  fi

  echo "[crop-fleet] $(date -Is) dept $DEPT exited code=$code — pause 15s"
  sleep 15
done
