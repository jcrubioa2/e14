#!/usr/bin/env python3
"""Quarantine the non-standard-geometry actas and reset any votes on them.

Reads the format census manifest (scripts/acta_format_census.py), marks every non-normal
acta ``quarantined=1`` in the results DB, and erases any community votes on them (their
crops are unreadable, so the votes are meaningless). Quarantined actas stay visible — the
webapp shows their crops with a notice and disables voting (see e14detector/webapp.py).

    # dry run: show what would change
    .venv/bin/python scripts/quarantine_nonnormal.py --dry-run

    # apply (local SQLite community store)
    .venv/bin/python scripts/quarantine_nonnormal.py \
        --results-db data/detector/results/results.sqlite \
        --community-db data/detector/community.sqlite

Votes go to Aurora in production: set AURORA_CLUSTER_ARN / AURORA_SECRET_ARN (+ vote AWS
creds) and omit --community-db; make_store then resets votes in Aurora.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14detector.community import make_store  # noqa: E402

DATA = ROOT / "data"
CENSUS = DATA / "format_census" / "manifest.json"


def nonnormal_ids(census: Path) -> list[str]:
    recs = json.loads(census.read_text())
    return sorted(r["document_id"] for r in recs if r["format"] in ("wide", "other"))


def mark_quarantined(results_db: Path, doc_ids: list[str], dry_run: bool) -> int:
    con = sqlite3.connect(results_db, timeout=60.0)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        if "quarantined" not in cols:
            if dry_run:
                print("  (would add 'quarantined' column to documents)")
            else:
                con.execute("ALTER TABLE documents ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0")
        present = {
            r[0] for r in con.execute(
                "SELECT document_id FROM documents WHERE document_id IN (%s)"
                % ",".join("?" * len(doc_ids)),
                doc_ids,
            )
        } if doc_ids else set()
        if dry_run:
            return len(present)
        con.executemany(
            "UPDATE documents SET quarantined=1 WHERE document_id=?", [(d,) for d in present]
        )
        con.commit()
        return len(present)
    finally:
        con.close()


def reset_votes(community_db: Path | None, doc_ids: list[str], dry_run: bool) -> dict[str, int]:
    if dry_run:
        return {}
    store = make_store(community_db)
    totals = {"flags": 0, "appeals": 0, "field_state": 0, "cid_index": 0}
    try:
        for doc_id in doc_ids:
            counts = store.delete_document(doc_id)
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v
    finally:
        store.close()
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--census", type=Path, default=CENSUS)
    ap.add_argument("--results-db", type=Path, default=DATA / "detector" / "results" / "results.sqlite")
    ap.add_argument("--community-db", type=Path, default=None,
                    help="SQLite community store; omit when using Aurora (env-configured)")
    ap.add_argument("--no-reset-votes", action="store_true", help="only quarantine, keep votes")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    doc_ids = nonnormal_ids(args.census)
    print(f"non-normal actas in census: {len(doc_ids)}")

    if not args.results_db.exists():
        print(f"ERROR: results DB not found: {args.results_db}", file=sys.stderr)
        return 1

    marked = mark_quarantined(args.results_db, doc_ids, args.dry_run)
    verb = "would mark" if args.dry_run else "marked"
    print(f"{verb} quarantined in {args.results_db}: {marked} (of {len(doc_ids)}; rest not in this DB)")

    if not args.no_reset_votes:
        totals = reset_votes(args.community_db, doc_ids, args.dry_run)
        if args.dry_run:
            print("(dry run) would reset votes for the quarantined actas")
        else:
            print(f"votes reset: {totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
