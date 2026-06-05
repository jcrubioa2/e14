#!/usr/bin/env bash
# SUPERSEDED by `e14 sync run --department <code>` (see docs/SYNC.md). Still works for a quick
# per-department crop push, but the unified tool also publishes the frontier DB + stamps the chain.
#
# Upload remaining candidate crops for one department (Ryzen/Legion divide-and-conquer).
#
#   bash scripts/publish_dept_crops.sh 16
#   WORKERS=64 bash scripts/publish_dept_crops.sh 16 --dry-run
#
# Run publish-reconcile first if this host's manifest may be behind the bucket.
set -euo pipefail
cd "$(dirname "$0")/.."

DEPT="${1:?usage: publish_dept_crops.sh <department-code> [--dry-run]}"
shift || true
DRY=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=(--dry-run) ;;
  esac
done

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

OUT="${DETECTOR_OUTPUT:-data/detector_national}"
PY="${E14_PY:-.venv/bin/e14detector}"
WORKERS="${WORKERS:-32}"

echo "publish-crops: department=${DEPT} output=${OUT} workers=${WORKERS}"
exec "$PY" publish-crops --output-dir "$OUT" --department "$DEPT" --workers "$WORKERS" "${DRY[@]}"
