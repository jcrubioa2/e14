#!/usr/bin/env python3
"""Depth-2: verify acta *bytes* against the frozen manifest baseline (catches content swaps).

The pointer diff (scripts/detect_silent_changes.py) only sees the registraduría's filename
hash move. This script proves the actual PDF bytes by re-downloading each selected mesa from
its CURRENT server URL, hashing the bytes, and comparing to the sha256 we froze in
data/manifest.db on Jun 1-2. It therefore catches both:
  - pointer-swap-with-content-change, and
  - content-only swaps (same filename hash, different bytes).

Caution / isolation (this script is strictly read-only against our wired-up data):
  - Downloads go to a throwaway temp dir (default data/_verify_tmp/, auto-cleaned), NEVER
    data/actas/. The real corpus is untouched.
  - manifest.db is opened read-only and never written. We reuse download_one() purely for its
    fetch+hash; we never call manifest.mark_* or run_download().
  - Verdicts come from comparing fresh sha256 vs manifest.sha256 in memory.

Selectors (choose what to verify; full re-crawl is heavy ~122k PDFs):
    .venv/bin/python scripts/verify_acta_content.py --report data/reports/silent_changes_*.csv
        # verify exactly the 'repointed' mesas a pointer diff flagged (the cheap, targeted run)
    .venv/bin/python scripts/verify_acta_content.py --sample 500
        # random 500 mesas -> statistical confidence that no content-only swaps are happening
    .venv/bin/python scripts/verify_acta_content.py --dep 16
        # every mesa in a department
    .venv/bin/python scripts/verify_acta_content.py --all --save-changed data/changed_full
        # full overnight re-verification; packs old.pdf+new.pdf per changed mesa for diffing

--save-changed <dir> writes <dir>/<key>/{old.pdf,new.pdf,INFO.txt} for every content_changed
mesa (old = our frozen data/actas copy, new = current server bytes). It refuses to write into
an existing folder. data/actas is read-only throughout.
"""
from __future__ import annotations

import argparse
import csv
import glob
import random
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14 import config  # noqa: E402
from e14.downloader import download_one  # noqa: E402
from e14.session import CdnSession  # noqa: E402
from e14.universe import ActaRecord, fetch_universe  # noqa: E402
from e14.util import RateLimiter  # noqa: E402

DATA = ROOT / "data"
MANIFEST_DB = DATA / "manifest.db"
ACTAS_ROOT = DATA / "actas"          # original baseline PDFs (read-only here)
REPORT_DIR = DATA / "reports"
DEFAULT_SCRATCH = DATA / "_verify_tmp"

# verdicts
MATCH = "match"                     # fresh bytes == frozen bytes  (clean)
CONTENT_CHANGED = "content_changed" # fresh sha256 != frozen sha256 (SILENT SWAP)
NO_BASELINE = "no_baseline"         # mesa absent from manifest (e.g. added after Jun 1-2)
ERROR = "error"                     # could not fetch / not a PDF


def load_manifest(db: Path) -> dict[str, dict]:
    """key -> {sha256, expected_name} from the frozen baseline (read-only)."""
    out: dict[str, dict] = {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for key, sha256, expected_name in con.execute(
            "SELECT key, sha256, expected_name FROM actas WHERE sha256 IS NOT NULL"
        ):
            out[key] = {"sha256": sha256, "expected_name": expected_name}
    finally:
        con.close()
    return out


def select_keys(args, manifest: dict[str, dict],
                universe: dict[str, ActaRecord]) -> list[str]:
    """Resolve the selector flags to a concrete list of keys present in the live universe."""
    if args.report:
        paths = sorted(glob.glob(args.report))
        if not paths:
            sys.exit(f"no report matched: {args.report}")
        path = paths[-1]
        keys = []
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("status") == "repointed":
                    keys.append(row["key"])
        print(f"selector: {len(keys)} repointed keys from {path}")
        return keys
    if args.keys:
        return [k.strip() for k in args.keys.split(",") if k.strip()]
    if args.dep:
        dep = args.dep.zfill(2)
        return [k for k in universe if k.split("_")[0] == dep]
    if args.sample:
        pool = list(universe.keys() & manifest.keys())  # only verifiable ones
        random.seed(args.seed)
        return random.sample(pool, min(args.sample, len(pool)))
    if args.all:
        return list(universe.keys())
    sys.exit("pick a selector: --report / --sample N / --dep DD / --keys ... / --all")


def _pack_changed(rec: ActaRecord, scratch: Path, save_dir: Path,
                  sha_old: str, sha_now: str) -> None:
    """Put the original (on-disk) and freshly fetched PDF side by side for easy diff.

    Layout: <save_dir>/<key>/{old.pdf,new.pdf,INFO.txt}. data/actas is only read.
    """
    rel = f"{rec.rel_dir()}/{rec.filename('delegados')}"
    fresh = scratch / rel                 # download_one just wrote this
    original = ACTAS_ROOT / rel           # our frozen Jun-1 copy
    dest = save_dir / rec.key
    dest.mkdir(parents=True, exist_ok=True)
    if fresh.exists():
        shutil.copy2(fresh, dest / "new.pdf")
    if original.exists():
        shutil.copy2(original, dest / "old.pdf")
    (dest / "INFO.txt").write_text(
        f"key={rec.key}\nurl={rec.pdf_url()}\n"
        f"sha_old={sha_old}\nsha_now={sha_now}\n"
        f"old_present={original.exists()}\n", encoding="utf-8")


def verify_one(key: str, universe: dict[str, ActaRecord], manifest: dict[str, dict],
               session: CdnSession, scratch: Path, save_dir: Path | None) -> dict:
    rec = universe.get(key)
    if rec is None:
        return {"key": key, "verdict": ERROR, "reason": "not in current universe"}
    base = manifest.get(key)
    # download_one re-fetches and hashes the bytes; writing to scratch keeps data/actas pristine.
    prov = download_one(rec, session, scratch, variant="delegados", force=True)
    fresh_sha = prov.get("sha256")
    pointer_now = rec.expected_name
    pointer_old = (base or {}).get("expected_name", "")
    row = {
        "key": key, "pointer_old": pointer_old, "pointer_now": pointer_now,
        "pointer_changed": str(bool(pointer_old) and pointer_old != pointer_now).lower(),
        "sha_old": (base or {}).get("sha256", ""), "sha_now": fresh_sha or "",
        "http_status": prov.get("http_status", ""), "reason": prov.get("reason", ""),
    }
    if prov.get("status") != "done" or not fresh_sha:
        row["verdict"] = ERROR
    elif base is None:
        row["verdict"] = NO_BASELINE
    elif fresh_sha == base["sha256"]:
        row["verdict"] = MATCH
    else:
        row["verdict"] = CONTENT_CHANGED
        if save_dir is not None:
            _pack_changed(rec, scratch, save_dir, row["sha_old"], fresh_sha)
    # keep scratch tiny (important for an --all overnight sweep): drop the fetched file
    fresh_path = scratch / f"{rec.rel_dir()}/{rec.filename('delegados')}"
    try:
        fresh_path.unlink()
    except OSError:
        pass
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("selectors")
    g.add_argument("--report", help="glob to a silent_changes report; verify its 'repointed' rows")
    g.add_argument("--sample", type=int, help="verify N random mesas (statistical sweep)")
    g.add_argument("--dep", help="verify every mesa in this department code")
    g.add_argument("--keys", help="comma-separated explicit keys")
    g.add_argument("--all", action="store_true", help="verify the entire universe (hours)")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed for --sample (reproducible)")
    ap.add_argument("--concurrency", type=int, default=config.DEFAULT_CONCURRENCY)
    ap.add_argument("--rate", type=float, default=config.DEFAULT_RATE_LIMIT)
    ap.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH,
                    help="throwaway download dir (auto-removed); never data/actas")
    ap.add_argument("--keep-scratch", action="store_true", help="don't delete downloaded PDFs")
    ap.add_argument("--save-changed", type=Path, default=None,
                    help="pack <dir>/<key>/{old.pdf,new.pdf} for each content_changed mesa. "
                         "Must NOT already exist (refuses to write into an existing folder).")
    args = ap.parse_args()

    save_dir = None
    if args.save_changed is not None:
        save_dir = args.save_changed
        if save_dir.exists():
            sys.exit(f"--save-changed dir already exists, refusing to write into it: {save_dir}")
        save_dir.mkdir(parents=True)
        print(f"changed actas will be packed -> {save_dir}/<key>/{{old,new}}.pdf")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # throwaway download dir under the (gitignored) base; never data/actas
    args.scratch.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"verify_{ts}_", dir=str(args.scratch)))
    print(f"scratch download dir: {scratch}  (data/actas untouched)")

    manifest = load_manifest(MANIFEST_DB)
    print(f"manifest baseline:    {len(manifest)} keys with sha256")
    print("fetching current universe ...")
    universe = {r.key: r for r in fetch_universe() if r.expected_name}
    print(f"current universe:     {len(universe)} keys")

    keys = select_keys(args, manifest, universe)
    print(f"verifying {len(keys)} mesas (concurrency={args.concurrency}, rate={args.rate}/s)")
    if not keys:
        print("nothing to verify.")
        return 0

    session = CdnSession(rate_limiter=RateLimiter(args.rate))
    session.prime()

    rows: list[dict] = []
    counts: Counter = Counter()
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(verify_one, k, universe, manifest, session, scratch, save_dir): k
                    for k in keys}
            for i, fut in enumerate(as_completed(futs), 1):
                row = fut.result()
                rows.append(row)
                counts[row["verdict"]] += 1
                if row["verdict"] == CONTENT_CHANGED:
                    print(f"  !! CONTENT_CHANGED {row['key']}  "
                          f"{row['sha_old'][:12]} -> {row['sha_now'][:12]}  "
                          f"(pointer_changed={row['pointer_changed']})")
                if i % 200 == 0:
                    print(f"  ... {i}/{len(keys)}")
    finally:
        if not args.keep_scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    report = REPORT_DIR / f"content_verify_{ts}.csv"
    cols = ["key", "verdict", "pointer_changed", "pointer_old", "pointer_now",
            "sha_old", "sha_now", "http_status", "reason"]
    with report.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in sorted(rows, key=lambda r: (r["verdict"] != CONTENT_CHANGED, r["key"])):
            w.writerow(row)

    print(f"\nverdicts ({len(rows)} verified):")
    for v in (CONTENT_CHANGED, MATCH, NO_BASELINE, ERROR):
        print(f"  {v:16} {counts.get(v, 0):>7}")
    print(f"report -> {report}")
    if counts.get(CONTENT_CHANGED):
        print(f"\n*** {counts[CONTENT_CHANGED]} mesa(s) changed bytes since the baseline — see report ***")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
