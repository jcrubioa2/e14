#!/usr/bin/env bash
# Quick national crop progress (actas + candidate PNG count + log tail).
set -euo pipefail
cd "$(dirname "$0")/.."

TOTAL="${E14_NATIONAL_TOTAL:-122007}"
OUT="${DETECTOR_OUTPUT:-data/detector_national}"
DB="$OUT/results/results.sqlite"
LOG="${CROP_LOG:-logs/national_crop.log}"
STATE="${CROP_PROGRESS_STATE:-/tmp/e14_crop_progress_state.json}"
# Set CROP_PROGRESS_FULL=1 to scan crops/ on disk (slow once millions of files exist).
FULL="${CROP_PROGRESS_FULL:-0}"
# Per-department table: CROP_PROGRESS_BY_DEPT=0 to hide; DEPT_LIMIT=N shows only incomplete depts.
BY_DEPT="${CROP_PROGRESS_BY_DEPT:-1}"
DEPT_LIMIT="${CROP_PROGRESS_DEPT_LIMIT:-0}"
UNIVERSE="${E14_UNIVERSE_CSV:-data/mesa_universe.csv}"

running() {
  pgrep -f "e14detector process" >/dev/null 2>&1 && echo "yes" || echo "no"
}

t0=$SECONDS
echo "=== E-14 crop progress ==="
echo "running: $(running)"
echo "output:  $OUT"

if [[ -f "$LOG" ]]; then
  last=$(grep -E 'processed [0-9]+/' "$LOG" | tail -1 || true)
  [[ -n "$last" ]] && echo "log:     $last (batch commits ~every 50 actas)"
else
  echo "log:     (no $LOG)"
fi

if [[ -f "$DB" ]]; then
  .venv/bin/python - <<PY
import csv
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

def fmt_duration(secs: float) -> str:
    mins = max(1, int(round(secs / 60)))
    hours, mins = divmod(mins, 60)
    if hours and mins:
        return f"~{hours}h {mins}m"
    if hours:
        return f"~{hours}h"
    return f"~{mins}m"

def rate_per_min(count: int, elapsed_secs: float) -> float | None:
    if count < 2 or elapsed_secs <= 0:
        return None
    return count / elapsed_secs * 60

db = Path("$DB")
state_path = Path("$STATE")
total = int("$TOTAL")
now = datetime.now(timezone.utc)
now_ts = time.time()

c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10.0)
c.row_factory = sqlite3.Row
docs = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
fields = c.execute(
    "SELECT COUNT(*) FROM vote_fields WHERE row_type='candidate'"
).fetchone()[0]
pct = 100.0 * docs / total if total else 0
remaining = max(0, total - docs)
print(f"db:      {docs:,} / {total:,} actas ({pct:.1f}%)")
print(f"fields:  {fields:,} candidate vote_fields (≈ crops in DB)")

rates: list[tuple[str, float]] = []

# Wall-clock windows (responsive; ignores slow startup / restarts).
for label, seconds in (("5 min", 300), ("10 min", 600)):
    cutoff = (now - timedelta(seconds=seconds)).isoformat()
    n = c.execute(
        "SELECT COUNT(*) FROM documents WHERE processing_timestamp >= ?", (cutoff,)
    ).fetchone()[0]
    r = rate_per_min(n, seconds)
    if r:
        rates.append((label, r))
        print(f"rate:    {r:.0f} actas/min (last {label})")

# Last N commits by rowid (matches bursty parallel commits).
n_recent = 1500
rows = c.execute(
    "SELECT processing_timestamp FROM documents ORDER BY rowid DESC LIMIT ?", (n_recent,)
).fetchall()
if len(rows) >= 2:
    def parse_ts(s: str) -> datetime:
        t = datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    t_first = parse_ts(rows[-1][0])
    t_last = parse_ts(rows[0][0])
    elapsed = (t_last - t_first).total_seconds()
    r = rate_per_min(len(rows), elapsed)
    if r:
        rates.append((f"last {len(rows)} actas", r))
        print(f"rate:    {r:.0f} actas/min (last {len(rows)} actas)")

# Instant rate vs previous script run (best match for \`watch -n 30\`).
prev_docs, prev_ts = 0, now_ts
if state_path.exists():
    try:
        st = json.loads(state_path.read_text())
        prev_docs = int(st.get("docs", 0))
        prev_ts = float(st.get("t", now_ts))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
dt = now_ts - prev_ts
if prev_docs > 0 and docs > prev_docs and dt >= 3:
    r_inst = (docs - prev_docs) / dt * 60
    rates.append(("since last check", r_inst))
    print(f"rate:    {r_inst:.0f} actas/min (since last check, {dt:.0f}s ago)")

state_path.write_text(json.dumps({"docs": docs, "t": now_ts}))

# ETA from the last-5-min window (stable). Instant rate is shown but not used — it
# spikes between 30s refreshes because the DB commits in ~50-acta bursts.
eta_rate = None
for label, r in rates:
    if label == "5 min":
        eta_rate = r
        break
if eta_rate is None and rates:
    eta_rate = rates[0][1]  # fallback if <5 min of data

if eta_rate and remaining > 0:
    eta_secs = remaining / (eta_rate / 60)
    if eta_secs <= 14 * 24 * 3600:
        print(f"eta:     {fmt_duration(eta_secs)} remaining (at {eta_rate:.0f}/min, last 5 min)")
    else:
        print("eta:     (unstable — wait for a longer recent window)")
elif remaining == 0:
    print("eta:     done")
else:
    print("eta:     (warming up — run again in ~30s)")

# Lifetime average (often pessimistic after a slow start / restart).
row = c.execute(
    "SELECT MIN(processing_timestamp) AS a, MAX(processing_timestamp) AS b "
    "FROM documents WHERE processing_timestamp IS NOT NULL"
).fetchone()
if docs >= 2 and row["a"] and row["b"]:
    try:
        t0 = datetime.fromisoformat(row["a"])
        t1 = datetime.fromisoformat(row["b"])
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        if t1.tzinfo is None:
            t1 = t1.replace(tzinfo=timezone.utc)
        elapsed = (t1 - t0).total_seconds()
        r_life = rate_per_min(docs, elapsed)
        if r_life and remaining > 0:
            print(
                f"eta_avg: {fmt_duration(remaining / (r_life / 60))} "
                f"(lifetime {r_life:.0f}/min — includes slow startup)"
            )
    except ValueError:
        pass

by_dept = "${BY_DEPT}" not in ("0", "false", "False")
dept_limit = int("${DEPT_LIMIT}" or "0")
universe_path = Path("${UNIVERSE}")
if by_dept and universe_path.is_file():
    expected = Counter()
    with universe_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            expected[str(row["dep"]).zfill(2)] += 1
    done_rows = c.execute(
        "SELECT department_code, COUNT(*) AS n, MAX(department_name) AS name "
        "FROM documents WHERE department_code IS NOT NULL AND department_code != '' "
        "GROUP BY department_code"
    ).fetchall()
    done_map = {
        str(r["department_code"]).zfill(2): (int(r["n"]), r["name"] or "")
        for r in done_rows
    }
    rows = []
    for dep in sorted(expected):
        exp = expected[dep]
        dn, name = done_map.get(dep, (0, ""))
        pct = min(100.0, 100.0 * dn / exp) if exp else 0.0
        rows.append((dep, name, dn, exp, pct))
    complete = sum(1 for _, _, dn, exp, _ in rows if dn >= exp)
    lowest = min(rows, key=lambda r: r[4]) if rows else None
    print("--- by department ---")
    shown = 0
    for dep, name, dn, exp, pct in rows:
        if dept_limit > 0 and pct >= 100.0:
            continue
        if dept_limit > 0 and shown >= dept_limit:
            break
        label = (name or "?")[:18]
        flag = " *" if dn > exp else ""
        print(f"  {dep}  {label:18}  {dn:6,} / {exp:6,}  {pct:5.1f}%{flag}")
        shown += 1
    if dept_limit > 0 and shown < len([r for r in rows if r[4] < 100.0]):
        rest = len([r for r in rows if r[4] < 100.0]) - shown
        if rest > 0:
            print(f"  ... {rest} more incomplete (raise CROP_PROGRESS_DEPT_LIMIT)")
    print(f"depts:   {complete}/{len(rows)} at 100%")
    if lowest:
        d, nm, dn, exp, pct = lowest
        print(f"lowest:  {d} {(nm or '?')[:12]} {pct:.1f}% ({dn:,}/{exp:,})")
elif by_dept and not universe_path.is_file():
    print(f"depts:   (no {universe_path} — run: make universe)")
PY
else
  echo "db:      (no results.sqlite yet)"
fi

if [[ "$FULL" == "1" && -d "$OUT/crops" ]]; then
  n=$(find "$OUT/crops" -name '*candidate*_field.png' 2>/dev/null | wc -l)
  echo "crops:   ${n// /,} candidate field PNGs on disk (full scan)"
  du -sh "$OUT/crops" 2>/dev/null | awk '{print "disk:    "$1" in crops/"}'
fi

elapsed=$((SECONDS - t0))
echo "=========================="
echo "refresh:  watch -n 30 'make detector-crop-progress DETECTOR_OUTPUT=$OUT'"
if [[ "$elapsed" -ge 8 ]]; then
  echo "note:     script took ${elapsed}s (skip disk scan; default is fast)"
fi
exit 0
