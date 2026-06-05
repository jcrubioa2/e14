#!/usr/bin/env bash
# Start crop supervisor (pull-db → crop loop). Needs data/actas PDFs; .env optional for crop-only.
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/crop_worker_env.sh

pdf_count=$(find "$E14_CROP_INPUT_DIR" -name '*.pdf' 2>/dev/null | wc -l)
if [[ "$pdf_count" -lt 1000 ]]; then
  echo "Missing PDFs in $E14_CROP_INPUT_DIR (found $pdf_count; expect ~122007)." >&2
  echo "  bash scripts/pull_from_lead.sh   # rsync from lead machine" >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "Note: no .env — crop-only OK; pull-db/publish need Tigris creds." >&2
fi

mkdir -p logs
echo "Starting crop supervisor: worker=$E14_WORKER_ID depts=$E14_DEPT_FROM-$E14_DEPT_TO workers=$E14_CROP_WORKERS"
exec bash scripts/crop_supervisor.sh
