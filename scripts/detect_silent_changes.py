#!/usr/bin/env python3
"""Detect silent registraduría changes to the acta universe (pointer-level diff).

The registraduría can quietly re-issue / replace acta PDFs after the fact. Each acta is
served under an opaque filename hash (`expected_name` == "{hash}.pdf"); if they swap a PDF
they mint a new hash for that mesa. This script compares the registraduría's *current*
pointer for every mesa against the baselines we froze locally on Jun 1-3, and lists every
mesa whose pointer moved (or that appeared / disappeared from the universe).

Baselines (all on disk; data/ is gitignored so there is no git history to diff):
  - data/manifest.db -> actas        T0  fetched 2026-06-01..02  expected_name + sha256
  - data/mesa_universe.csv           T1  2026-06-03              expected_name
Fresh:
  - live fetch_universe()            T2  now                     expected_name

All three reconcile to the canonical ActaRecord.key = dep2_muni3_zona3_puesto2_mesa3.
The manifest also froze the true content sha256 of every PDF's bytes; this script only does
the pointer diff — proving the *bytes* changed (re-download + re-hash vs manifest.sha256) is
a cheap follow-up over the repointed set produced here.

    .venv/bin/python scripts/detect_silent_changes.py            # live diff vs now
    .venv/bin/python scripts/detect_silent_changes.py --offline  # T0 vs T1 only, no network
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14.universe import (  # noqa: E402
    ActaRecord,
    fetch_universe,
    load_universe_csv,
    write_universe_csv,
)

DATA = ROOT / "data"
MANIFEST_DB = DATA / "manifest.db"
UNIVERSE_CSV = DATA / "mesa_universe.csv"
INDEX_CSV = DATA / "index.csv"
SNAP_DIR = DATA / "snapshots"
REPORT_DIR = DATA / "reports"

# status buckets for a diff(old -> new)
REPOINTED = "repointed"   # in both, pointer hash changed  -> silent swap signal
ADDED = "added"           # in new only                    -> new mesa
REMOVED = "removed"       # in old only                    -> mesa vanished
UNCHANGED = "unchanged"   # same pointer


def load_manifest(db: Path) -> dict[str, dict]:
    """key -> {pointer, sha256, source_url, fetched_at} from the manifest baseline (T0)."""
    out: dict[str, dict] = {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT key, expected_name, sha256, source_url, fetched_at_utc "
            "FROM actas WHERE expected_name IS NOT NULL AND expected_name <> ''"
        )
        for key, expected_name, sha256, source_url, fetched_at in cur:
            out[key] = {
                "pointer": expected_name,
                "sha256": sha256,
                "source_url": source_url,
                "fetched_at": fetched_at,
            }
    finally:
        con.close()
    return out


def universe_pointers(records: list[ActaRecord]) -> dict[str, str]:
    """key -> pointer hash filename, for a list of ActaRecords (T1 / T2)."""
    return {r.key: r.expected_name for r in records if r.expected_name}


def _key_parts(key: str) -> tuple[str, str, str, str, str]:
    dep, muni, zona, puesto, mesa = key.split("_")
    return dep, muni, zona, puesto, mesa


def diff(old: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    """key -> status, comparing two pointer maps."""
    out: dict[str, str] = {}
    for key in old.keys() | new.keys():
        o, n = old.get(key), new.get(key)
        if o is None:
            out[key] = ADDED
        elif n is None:
            out[key] = REMOVED
        elif o != n:
            out[key] = REPOINTED
        else:
            out[key] = UNCHANGED
    return out


def summarize(label: str, statuses: dict[str, str]) -> None:
    c = Counter(statuses.values())
    total = sum(c.values())
    print(f"\n{label}  (total keys: {total})")
    for status in (REPOINTED, ADDED, REMOVED, UNCHANGED):
        print(f"  {status:10} {c.get(status, 0):>7}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="skip the live fetch; diff manifest (T0) vs mesa_universe.csv (T1) only")
    ap.add_argument("--manifest", type=Path, default=MANIFEST_DB)
    ap.add_argument("--universe", type=Path, default=UNIVERSE_CSV)
    args = ap.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = SNAP_DIR / ts
    snap.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. preserve baselines immutably (never mutated by this script; copy for the record)
    for src in (args.universe, INDEX_CSV):
        if src.exists():
            shutil.copy2(src, snap / src.name)
    print(f"baselines copied -> {snap}")

    # 2. load pointer maps
    t0 = load_manifest(args.manifest)               # Jun 1-2
    print(f"T0 manifest:        {len(t0):>7} keys  ({args.manifest})")
    t1_recs = load_universe_csv(args.universe)
    t1 = universe_pointers(t1_recs)                  # Jun 3
    print(f"T1 mesa_universe:   {len(t1):>7} keys  ({args.universe})")

    t0_ptr = {k: v["pointer"] for k, v in t0.items()}

    if args.offline:
        t2 = None
    else:
        print("T2 fetching live universe (allTransmissionCodes.json) ...")
        t2_recs = fetch_universe()
        write_universe_csv(t2_recs, snap / "mesa_universe.fresh.csv")
        t2 = universe_pointers(t2_recs)
        print(f"T2 live now:        {len(t2):>7} keys  ({snap / 'mesa_universe.fresh.csv'})")

    # 3. diffs
    drift_01 = diff(t0_ptr, t1)                       # Jun1 -> Jun3 (offline)
    summarize("T0->T1  Jun 1-2 manifest  ->  Jun 3 universe", drift_01)

    if t2 is not None:
        # newest baseline -> now. prefer T1 (Jun3) as the "old" side since it's the latest frozen.
        current = diff(t1, t2)                        # Jun3 -> now
        summarize("T1->T2  Jun 3 universe  ->  NOW (live)", current)
        authoritative = current
        old_for_report = t1
        new_for_report = t2
    else:
        authoritative = drift_01
        old_for_report = t0_ptr
        new_for_report = t1

    # 4. report
    report = REPORT_DIR / f"silent_changes_{ts}.csv"
    all_keys = sorted(old_for_report.keys() | new_for_report.keys())
    with report.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "key", "dep", "muni", "zona", "puesto", "mesa", "status",
            "pointer_jun1", "pointer_jun3", "pointer_now",
            "source_url_jun1", "content_sha256_jun1", "fetched_at_jun1",
        ])
        for key in all_keys:
            dep, muni, zona, puesto, mesa = _key_parts(key)
            m = t0.get(key, {})
            w.writerow([
                key, dep, muni, zona, puesto, mesa, authoritative[key],
                t0_ptr.get(key, ""), t1.get(key, ""),
                (t2.get(key, "") if t2 is not None else ""),
                m.get("source_url", ""), m.get("sha256", ""), m.get("fetched_at", ""),
            ])
    print(f"\nreport -> {report}")

    # eyeball sample
    repointed = [k for k, s in authoritative.items() if s == REPOINTED]
    if repointed:
        print(f"\nfirst repointed mesas ({len(repointed)} total):")
        for key in sorted(repointed)[:20]:
            old = old_for_report.get(key, "")
            new = new_for_report.get(key, "")
            print(f"  {key}  {old}  ->  {new}")
    else:
        print("\nno repointed mesas in the authoritative comparison "
              "(no silent re-issues detected vs the baseline).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
