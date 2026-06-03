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
# Leave cores free for interactive use: pin to a core subset and run at low priority so
# foreground work always wins. Re-applied on every respawn below. The core set comes from
# (in order) the E14_PL_CPU_SET env, the .cpu_set override file (written by
# scripts/cpu_throttle.sh so a live retune survives respawns), or the 0-9 default.
CPU_SET="${E14_PL_CPU_SET:-$(cat "$(dirname "$0")/../.cpu_set" 2>/dev/null || echo 0-9)}"
NICE="${E14_PL_NICE:-15}"
# nice 0 (normal priority) when running on the full machine, low priority when throttled.
[[ "$CPU_SET" == "0-11" ]] && NICE="${E14_PL_NICE:-0}"

echo "[supervisor] $(date -Is) starting (dir=$OUTPUT_DIR workers=$WORKERS limit=$UPLOAD_LIMIT interval=$INTERVAL db=$DB_INTERVAL cpus=$CPU_SET nice=$NICE)"
while true; do
  taskset -c "$CPU_SET" nice -n "$NICE" "$PY" publish-loop \
    --output-dir "$OUTPUT_DIR" \
    --workers "$WORKERS" \
    --upload-limit "$UPLOAD_LIMIT" \
    --interval "$INTERVAL" \
    --db-interval "$DB_INTERVAL"
  code=$?
  echo "[supervisor] $(date -Is) loop exited (code=$code) — restarting in 5s"
  sleep 5
done
