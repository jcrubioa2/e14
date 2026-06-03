#!/usr/bin/env bash
# Restart the national crop run if it exits (reboot, OOM, terminal closed).
# Pulls the live published DB first so multiple PCs do not crop the same actas.
#
#   nohup bash scripts/crop_supervisor.sh >> logs/crop_supervisor.log 2>&1 & disown
#
# Tunables:
#   E14_CROP_WORKERS, E14_CROP_OUTPUT_DIR, E14_CROP_INPUT_DIR
#   E14_DEPT_FROM / E14_DEPT_TO / E14_DEPTO  — department slice (see docs/MULTI_MACHINE.md)
#   E14_WORKER_ID — label in logs (e.g. pc2)
set -u
cd "$(dirname "$0")/.."

WORKERS="${E14_CROP_WORKERS:-32}"
INPUT="${E14_CROP_INPUT_DIR:-data/actas}"
OUTPUT="${E14_CROP_OUTPUT_DIR:-data/detector_national}"
PY="${E14_CROP_PY:-.venv/bin/e14detector}"
WORKER="${E14_WORKER_ID:-$(hostname -s)}"
DEPT_ARGS=()
if [[ -n "${E14_DEPTO:-}" ]]; then
  DEPT_ARGS+=(--depto "$E14_DEPTO")
elif [[ -n "${E14_DEPT_FROM:-}" || -n "${E14_DEPT_TO:-}" ]]; then
  [[ -n "${E14_DEPT_FROM:-}" ]] && DEPT_ARGS+=(--dept-from "$E14_DEPT_FROM")
  [[ -n "${E14_DEPT_TO:-}" ]] && DEPT_ARGS+=(--dept-to "$E14_DEPT_TO")
fi

echo "[crop-supervisor] $(date -Is) worker=$WORKER workers=$WORKERS in=$INPUT out=$OUTPUT depts=${E14_DEPTO:-${E14_DEPT_FROM:-*}-${E14_DEPT_TO:-*}}"
while true; do
  echo "[crop-supervisor] $(date -Is) pull-db"
  "$PY" pull-db --output-dir "$OUTPUT" >> logs/national_crop.log 2>&1 || true
  echo "[crop-supervisor] $(date -Is) launching process"
  "$PY" process --input-dir "$INPUT" --output-dir "$OUTPUT" --workers "$WORKERS" --crop-only \
    "${DEPT_ARGS[@]}" >> logs/national_crop.log 2>&1
  code=$?
  echo "[crop-supervisor] $(date -Is) exited code=$code — restart in 15s"
  sleep 15
done
