#!/usr/bin/env bash
# Upload progress: manifest uploaded keys vs candidate crops in the results DB.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${DETECTOR_OUTPUT:-data/detector_national}"
MAN="$OUT/review/uploaded_crops.txt"
DB="$OUT/results/results.sqlite"
STATE="${PUBLISH_PROGRESS_STATE:-/tmp/e14_upload_progress.json}"
LOG="${PUBLISH_LOG:-logs/publish_loop.log}"
PY="${E14_CROP_PY:-.venv/bin/python}"

fmt_duration() {
  local secs=${1:-0}
  local mins=$(( (secs + 30) / 60 ))
  local hours=$(( mins / 60 ))
  mins=$(( mins % 60 ))
  if (( hours > 0 && mins > 0 )); then
    printf '~%dh %dm' "$hours" "$mins"
  elif (( hours > 0 )); then
    printf '~%dh' "$hours"
  else
    printf '~%dm' "$mins"
  fi
}

up=0
if [[ -f "$MAN" ]]; then
  up=$(wc -l < "$MAN" | tr -d ' ')
fi

total=0
if [[ -f "$DB" ]]; then
  total=$("$PY" -c "
import sqlite3
from pathlib import Path
db = Path('$DB')
con = sqlite3.connect(f'file:{db.resolve()}?mode=ro', uri=True, timeout=30.0)
print(con.execute(\"SELECT COUNT(*) FROM vote_fields WHERE row_type='candidate'\").fetchone()[0])
")
fi

rem=$(( total > up ? total - up : 0 ))
now=$(date +%s)
pct="0.0"
if (( total > 0 )); then
  pct=$(awk -v u="$up" -v t="$total" 'BEGIN {printf "%.1f", 100*u/t}')
fi

echo "=== E-14 upload progress ==="
echo "output:  $OUT"
printf "upload:  %'d / %'d crops (%s%%)\n" "$up" "$total" "$pct"
printf "left:    %'d\n" "$rem"
if pgrep -f "publish-loop|publish-crops|publish_supervisor" >/dev/null 2>&1; then
  echo "running: yes"
else
  echo "running: no"
fi

prev_up=0
prev_t=$now
if [[ -f "$STATE" ]]; then
  read -r prev_up prev_t < "$STATE" 2>/dev/null || true
fi
dt=$((now - prev_t))
du=$((up - prev_up))
if (( dt >= 3 && du > 0 )); then
  rate=$(awk -v d="$du" -v s="$dt" 'BEGIN {printf "%.0f", d/s*60}')
  echo "rate:    ${rate}/min (since last check, ${dt}s ago)"
  if (( rem > 0 )); then
    eta_secs=$(awk -v d="$du" -v s="$dt" -v rem="$rem" 'BEGIN {if(d<=0) print 0; else print rem/(d/s)}')
    echo "eta:     $(fmt_duration "${eta_secs%.*}") remaining (at ${rate}/min)"
  else
    echo "eta:     done"
  fi
elif (( rem == 0 && total > 0 )); then
  echo "eta:     done"
else
  echo "eta:     (warming up — run again in ~30s)"
fi

printf '%s %s\n' "$up" "$now" > "$STATE"

if [[ -f "$LOG" ]]; then
  last=$(grep -E '^\[publish-loop\]|^publish-crops:' "$LOG" | tail -1 || true)
  [[ -n "$last" ]] && echo "log:     $last"
fi
echo "=========================="
echo "refresh: watch -n 30 bash scripts/publish_progress.sh"
