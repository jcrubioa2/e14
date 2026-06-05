#!/usr/bin/env bash
# Per-department progress: universe vs local DB vs Tigris published DB vs CDN upload frontier.
#
#   bash scripts/publish_status_by_dept.sh              # once
#   bash scripts/publish_status_by_dept.sh --watch      # live (default 90s)
#   WATCH_INTERVAL=60 bash scripts/publish_status_by_dept.sh --watch
#   bash scripts/publish_status_by_dept.sh --sync-bucket   # foreground bucket list (~8 min)
#
# Bucket listing is slow (~1M keys). By default the table prints immediately using the cache
# (review/uploaded_crops_bucket.cache) while a background sync refreshes it when stale.
# E14_STATUS_BUCKET_SYNC=0 uses local manifest only (instant, Ryzen-only cdn_ok).
# Requires: .env for CDN + optional Tigris creds; network for db/latest.json (+ snapshot when pointer changes).
set -euo pipefail
cd "$(dirname "$0")/.."

WATCH=0
SYNC_BUCKET_FG=0
INTERVAL="${WATCH_INTERVAL:-90}"
for arg in "$@"; do
  case "$arg" in
    --watch|-w) WATCH=1 ;;
    --sync-bucket) SYNC_BUCKET_FG=1 ;;
    -h|--help)
      echo "Usage: bash scripts/publish_status_by_dept.sh [--watch] [--sync-bucket]"
      echo "  --watch          refresh loop (WATCH_INTERVAL seconds, default 90)"
      echo "  --sync-bucket    block ~8 min to refresh bucket cache before showing table"
      echo "  WATCH_INTERVAL   seconds between refreshes"
      echo "  E14_STATUS_BUCKET_SYNC=0   local manifest only (no bucket cache)"
      exit 0
      ;;
  esac
done

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

OUT="${DETECTOR_OUTPUT:-data/detector_national}"
PY="${E14_STATUS_PY:-.venv/bin/python3}"
UNIVERSE="${E14_UNIVERSE_CSV:-data/mesa_universe.csv}"
CDN="${E14_CDN_BASE_URL:-https://e14-crops.fly.storage.tigris.dev}"
STATE="${E14_PUBLISH_STATUS_STATE:-/tmp/e14_publish_status_state.json}"
export E14_STATUS_BUCKET_SYNC="${E14_STATUS_BUCKET_SYNC:-1}"
export E14_BUCKET_SYNC_INTERVAL="${E14_BUCKET_SYNC_INTERVAL:-600}"
BUCKET_LOCK="${E14_BUCKET_SYNC_LOCK:-/tmp/e14_bucket_sync.lock}"
BUCKET_CACHE="${OUT}/review/uploaded_crops_bucket.cache"

needs_background_bucket_sync() {
  [[ "${E14_STATUS_BUCKET_SYNC:-1}" == "0" ]] && return 1
  [[ ! -f "$BUCKET_CACHE" ]] && return 0
  local age=$(( $(date +%s) - $(stat -c %Y "$BUCKET_CACHE" 2>/dev/null || echo 0) ))
  (( age >= E14_BUCKET_SYNC_INTERVAL ))
}

start_background_bucket_sync() {
  [[ "${E14_STATUS_BUCKET_SYNC:-1}" == "0" ]] && return 0
  mkdir -p logs
  # flock: only one list-objects run at a time
  flock -n "$BUCKET_LOCK" bash -c "
    set -a
    [[ -f .env ]] && source .env
    set +a
    echo \"[\$(date -Iseconds)] bucket cache sync start\" >> logs/bucket_sync.log
    '$PY' -c \"
from pathlib import Path
from e14detector.publish import refresh_bucket_upload_cache
p = Path('$BUCKET_CACHE')
refresh_bucket_upload_cache(p, verbose=True)
print(f'cache written: {p}', flush=True)
\" >> logs/bucket_sync.log 2>&1
    echo \"[\$(date -Iseconds)] bucket cache sync done\" >> logs/bucket_sync.log
  " &>/dev/null &
}

run_foreground_bucket_sync() {
  echo "syncing crop keys from Tigris (foreground, ~8 min)…" >&2
  mkdir -p logs
  flock "$BUCKET_LOCK" bash -c "
    set -a
    [[ -f .env ]] && source .env
    set +a
    '$PY' -c \"
from pathlib import Path
from e14detector.publish import refresh_bucket_upload_cache
refresh_bucket_upload_cache(Path('$BUCKET_CACHE'), verbose=True)
\"
  "
}

run_once() {
  if [[ "$SYNC_BUCKET_FG" -eq 1 ]]; then
    run_foreground_bucket_sync
  elif needs_background_bucket_sync; then
    start_background_bucket_sync
  fi
  export E14_BUCKET_SYNC_LOCK="$BUCKET_LOCK"
  if flock -n "$BUCKET_LOCK" true 2>/dev/null; then
    export E14_BUCKET_SYNC_RUNNING=0
  else
    export E14_BUCKET_SYNC_RUNNING=1
  fi
  "$PY" - "$OUT" "$UNIVERSE" "$CDN" "$STATE" <<'PY'
import csv
import gzip
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

out = Path(sys.argv[1])
universe_csv = Path(sys.argv[2])
cdn_base = sys.argv[3].rstrip("/")
state_path = Path(sys.argv[4])
loc_db = out / "results" / "results.sqlite"
manifest = out / "review" / "uploaded_crops.txt"
bucket_cache = out / "review" / "uploaded_crops_bucket.cache"
bucket_sync = os.environ.get("E14_STATUS_BUCKET_SYNC", "1") not in ("0", "false", "no")
bucket_interval = float(os.environ.get("E14_BUCKET_SYNC_INTERVAL", "600"))
bucket_lock = Path(os.environ.get("E14_BUCKET_SYNC_LOCK", "/tmp/e14_bucket_sync.lock"))

if not universe_csv.is_file():
    print(f"missing {universe_csv} (run: make universe)", file=sys.stderr)
    sys.exit(1)
if not loc_db.is_file():
    print(f"missing {loc_db}", file=sys.stderr)
    sys.exit(1)

exp = Counter()
with universe_csv.open(encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        exp[str(row["dep"]).zfill(2)] += 1

state: dict = {}
if state_path.exists():
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        state = {}

published: dict[str, int] = dict(state.get("published_by_dep") or {})
ptr_info = state.get("ptr_info", "unavailable")
ptr_sha = state.get("ptr_sha", "")

try:
    ptr = json.loads(
        urllib.request.urlopen(f"{cdn_base}/db/latest.json?t={int(time.time())}", timeout=30).read()
    )
    new_sha = ptr.get("sha256", "")
    if new_sha != ptr_sha:
        with urllib.request.urlopen(f"{cdn_base}/{ptr['key']}", timeout=180) as resp:
            with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
                with gzip.GzipFile(fileobj=resp) as gz:
                    tmp.write(gz.read())
                tmp.flush()
                con = sqlite3.connect(tmp.name)
                published = {
                    str(r[0]).zfill(2): int(r[1])
                    for r in con.execute(
                        "SELECT department_code, COUNT(*) FROM documents "
                        "WHERE department_code IS NOT NULL AND department_code != '' "
                        "GROUP BY department_code"
                    )
                }
                con.close()
        ptr_sha = new_sha
        ptr_info = f"sha={new_sha[:12]}  key={ptr['key']}"
except Exception as exc:
    print(f"warning: published DB ({type(exc).__name__}: {exc})", file=sys.stderr)

con = sqlite3.connect(f"file:{loc_db}?mode=ro", uri=True, timeout=60.0)
local = {
    str(r[0]).zfill(2): int(r[1])
    for r in con.execute(
        "SELECT department_code, COUNT(*) FROM documents "
        "WHERE department_code IS NOT NULL AND department_code != '' "
        "GROUP BY department_code"
    )
}
from e14detector.webapp import crop_key

doc_keys: dict[str, list[str]] = defaultdict(list)
doc_dep: dict[str, str] = {}
for dep, doc_id, path in con.execute(
    "SELECT d.department_code, d.document_id, vf.raw_crop_path "
    "FROM documents d JOIN vote_fields vf ON vf.document_id = d.document_id "
    "WHERE vf.row_type='candidate' AND vf.raw_crop_path IS NOT NULL "
    "AND vf.raw_crop_path != ''"
):
    doc_dep[doc_id] = str(dep).zfill(2)
    doc_keys[doc_id].append(crop_key(path))
con.close()

local_manifest: set[str] = set()
if manifest.is_file():
    local_manifest = set(manifest.read_text(encoding="utf-8").splitlines())

bucket_keys: set[str] = set()
bucket_sync_note = ""
last_bucket_sync = float(state.get("bucket_sync_t", 0))
if bucket_cache.is_file():
    bucket_keys = set(bucket_cache.read_text(encoding="utf-8").splitlines())

def _cache_age() -> int:
    if bucket_cache.is_file():
        return int(time.time() - bucket_cache.stat().st_mtime)
    return -1

cache_age = _cache_age()
sync_running = os.environ.get("E14_BUCKET_SYNC_RUNNING") == "1"
if bucket_keys:
    bucket_sync_note = f"bucket cache {len(bucket_keys):,} keys ({cache_age}s old)"
    if bucket_sync and (cache_age >= bucket_interval or sync_running):
        note = "background refresh running" if sync_running else "background refresh due"
        bucket_sync_note += f" — {note} (logs/bucket_sync.log)"
elif bucket_sync:
    bucket_sync_note = "no bucket cache yet — manifest-only cdn_ok"
    if sync_running:
        bucket_sync_note += " (background sync running: logs/bucket_sync.log)"

uploaded = local_manifest | bucket_keys if bucket_keys else local_manifest

frontier = Counter(
    doc_dep[d]
    for d, keys in doc_keys.items()
    if keys and all(k in uploaded for k in keys)
)

state.update({
    "ptr_sha": ptr_sha,
    "ptr_info": ptr_info,
    "published_by_dep": published,
    "t": time.time(),
})
try:
    state_path.write_text(json.dumps(state), encoding="utf-8")
except OSError:
    pass

print("=== publish status by department ===")
print(f"refresh:   {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"output:    {out}")
print(f"cdn:       {cdn_base}")
print(f"pointer:   {ptr_info}")
print(f"manifest:  {len(local_manifest):,} crop keys (this host publish-loop)")
if bucket_keys:
    only_bucket = len(bucket_keys - local_manifest)
    print(f"bucket:    {len(bucket_keys):,} crop keys on Tigris ({only_bucket:,} not in local manifest)")
    print(f"cdn_ok:    uses merged bucket+local keys — includes Legion/other machines")
elif bucket_sync:
    print("bucket:    (not synced — set BUCKET_NAME/AWS_* in .env or run publish-reconcile)")
else:
    print("cdn_ok:    local manifest only (E14_STATUS_BUCKET_SYNC=0)")
if bucket_sync_note:
    print(f"sync:      {bucket_sync_note}")
print()
print(f"{'dep':>4}  {'univ':>7}  {'local':>7}  {'published':>9}  {'cdn_ok':>7}  {'crop%':>6}")
shown = 0
for dep in sorted(exp):
    e = exp[dep]
    l = local.get(dep, 0)
    p = published.get(dep, l) if published else -1
    f = frontier.get(dep, 0)
    crop_pct = f"{100 * f / l:.0f}%" if l else "n/a"
    pub_s = f"{p:>9,}" if p >= 0 else "      n/a"
    if l < e or (published and p < e) or f < l:
        print(f"{dep:>4}  {e:>7,}  {l:>7,}  {pub_s}  {f:>7,}  {crop_pct:>6}")
        shown += 1
if shown == 0:
    print("(all departments at 100% local/published vs universe, crops fully on CDN)")
print()
pub_total = f"{sum(published.values()):,}" if published else "n/a"
print(
    f"totals: universe={sum(exp.values()):,}  local={sum(local.values()):,}  "
    f"published={pub_total}  cdn_frontier_actas={sum(frontier.values()):,}"
)
track_n = len(bucket_keys) if bucket_keys else len(local_manifest)
if state.get("manifest_n") is not None and track_n > int(state["manifest_n"]):
    dn = track_n - int(state["manifest_n"])
    dt = max(1, time.time() - float(state.get("t_prev", state["t"])))
    label = "bucket" if bucket_keys else "manifest"
    print(f"rate:      +{dn:,} crops on {label} since last refresh ({dn / dt * 60:.0f}/min)")
state["manifest_n"] = track_n
state["t_prev"] = time.time()
try:
    state_path.write_text(json.dumps(state), encoding="utf-8")
except OSError:
    pass
PY
}

if [[ "$WATCH" -eq 1 ]]; then
  echo "Live publish status every ${INTERVAL}s (Ctrl+C to stop). Uses bucket cache; stale cache refreshes in background."
  while true; do
    clear 2>/dev/null || true
    run_once
    echo ""
    echo "next refresh in ${INTERVAL}s — watch: bash scripts/publish_status_by_dept.sh --watch"
    sleep "$INTERVAL"
  done
else
  run_once
fi
