#!/usr/bin/env bash
# Supervises the crop/DB publish loop so the public site keeps growing unattended.
#
# The loop itself is crash-proof per-cycle (try/except), but if the process is ever
# killed (terminal close, OOM, manual kill) nothing restarts it. This wrapper:
#   - loads S3/Tigris creds from .env,
#   - (re)launches the loop and restarts it after any exit,
#   - is meant to be run detached:  nohup bash scripts/publish_supervisor.sh >> logs/publish_loop.log 2>&1 & disown
#
# Tunables via env (defaults match the national rollout):
#   E14_PL_WORKERS, E14_PL_UPLOAD_LIMIT, E14_PL_INTERVAL, E14_PL_DB_INTERVAL
set -u
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a; . ./.env; set +a
fi

WORKERS="${E14_PL_WORKERS:-64}"
UPLOAD_LIMIT="${E14_PL_UPLOAD_LIMIT:-12000}"
INTERVAL="${E14_PL_INTERVAL:-20}"
DB_INTERVAL="${E14_PL_DB_INTERVAL:-120}"
# Must point at the national run, NOT the config default (data/detector holds a small
# dev stub — publishing it would shrink the live DB; the publish-db guard now blocks that,
# but pinning the right dir here avoids wasted cycles entirely).
OUTPUT_DIR="${E14_PL_OUTPUT_DIR:-data/detector_national}"
PY="${E14_PL_PY:-.venv/bin/e14detector}"

echo "[supervisor] $(date -Is) starting (dir=$OUTPUT_DIR workers=$WORKERS limit=$UPLOAD_LIMIT interval=$INTERVAL db=$DB_INTERVAL)"
while true; do
  "$PY" publish-loop \
    --output-dir "$OUTPUT_DIR" \
    --workers "$WORKERS" \
    --upload-limit "$UPLOAD_LIMIT" \
    --interval "$INTERVAL" \
    --db-interval "$DB_INTERVAL"
  code=$?
  echo "[supervisor] $(date -Is) loop exited (code=$code) — restarting in 5s"
  sleep 5
done
