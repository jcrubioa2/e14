#!/usr/bin/env bash
# Start fleet crop worker (needs PDFs + fleet queue). Sources crop_worker_env.sh for workers id.
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/crop_worker_env.sh

pdf_count=$(find "$E14_CROP_INPUT_DIR" -name '*.pdf' 2>/dev/null | wc -l)
if [[ "$pdf_count" -lt 1000 ]]; then
  echo "Missing PDFs in $E14_CROP_INPUT_DIR (found $pdf_count)." >&2
  echo "  E14_LEAD_HOST=ryzen9-1 bash scripts/pull_from_lead.sh" >&2
  exit 1
fi

mkdir -p logs
echo "Starting fleet worker: id=$E14_WORKER_ID workers=$E14_CROP_WORKERS"
exec bash scripts/crop_supervisor_fleet.sh
