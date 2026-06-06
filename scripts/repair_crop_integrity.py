#!/usr/bin/env python3
"""One-time repair: restore 13/13 candidate crops per acta in a ``results.sqlite``.

Two data bugs the ``recover9`` rebuild left behind (both verified — every acta is a
13-candidate presidential ``_delegados`` acta, none are genuinely missing rows):

  1. Under-count: ``candidate`` rows whose ``raw_crop_path`` is NULL/empty even though the
     crop PNG exists on disk (the rebuild dropped the column for those rows). Surfaces as
     actas reading 10/11/12 instead of 13. Fix: reconstruct the deterministic crop name
     (see ``cropper.save_field_crops``) under the crops directory of a sibling non-null row
     in the same document, verify the file exists, and backfill raw/enhanced/debug/slot paths.

  2. Over-count: a double-ingested batch (dept 27 / Quibdó / puesto 007) where every row was
     inserted twice — identical ``(document_id,page_number,row_number,section,row_type)`` with
     an identical ``raw_crop_path`` (13 candidate × 2 = 26). Fix: delete surplus rows keeping
     ``MIN(id)`` (the kept copy is arbitrary — duplicates are identical stubs). Child tables
     (cv_features / digit_comparisons / vlm_reviews) key on (document_id,page,row), not
     vote_fields.id, so dedupe orphans nothing.

After both fixes it recomputes ``documents.n_candidates`` (the column already exists, so
``ensure_n_candidates`` would short-circuit — we must recompute it ourselves) and rebuilds the
/browse indexes via ``ensure_browse_indexes``.

IMPORTANT — the two crop key namespaces: the DB stores the *readable* path; the public CDN key
is the *opaque* HMAC ``crops/<hmac>.png`` derived on the fly. Backfilling the path makes the app
compute the right opaque URL, but the 343 restored crops were never uploaded (the upload plan
filters ``raw_crop_path IS NOT NULL``) so their opaque object is absent in the bucket. After
``--apply`` you MUST run a normal crop publish (same env as publishing/rekey) so those crops are
uploaded under their opaque keys, then re-publish the DB snapshot.

Idempotent and dry-run by default:
  python -m scripts.repair_crop_integrity --db data/recover9/results/results.sqlite --dry-run
  python -m scripts.repair_crop_integrity --db data/recover9/results/results.sqlite --apply

Exit codes: 0 = clean; 3 = some NULL-path rows had NO crop file on disk (a *genuine* gap that
needs the deferred "imagen no disponible" UI, not a backfill).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from e14detector.webapp import ensure_browse_indexes

# Recompute documents.n_candidates exactly as ensure_n_candidates does (crop-backed candidate
# rows per acta) — but unconditionally, since the column already exists on a rebuilt snapshot.
_RECOMPUTE_N_CANDIDATES = (
    "UPDATE documents SET n_candidates = COALESCE("
    "(SELECT COUNT(*) FROM vote_fields vf WHERE vf.document_id = documents.document_id "
    "AND vf.row_type='candidate' AND vf.raw_crop_path IS NOT NULL AND vf.raw_crop_path != ''), 0)"
)

# Every row of a (document_id,page,row,section,row_type) group except the smallest id.
_SURPLUS_ROWS = (
    "SELECT v.id FROM vote_fields v WHERE EXISTS ("
    "SELECT 1 FROM vote_fields v2 WHERE v2.document_id=v.document_id "
    "AND v2.page_number=v.page_number AND v2.row_number=v.row_number "
    "AND v2.row_type=v.row_type AND IFNULL(v2.section,'')=IFNULL(v.section,'') "
    "AND v2.id < v.id)"
)


def _sibling_crops_dir(con: sqlite3.Connection, document_id: str) -> str | None:
    """Directory of a non-null candidate crop in the same document (env-agnostic prefix)."""
    row = con.execute(
        "SELECT raw_crop_path FROM vote_fields WHERE document_id=? AND row_type='candidate' "
        "AND raw_crop_path IS NOT NULL AND raw_crop_path != '' LIMIT 1",
        (document_id,),
    ).fetchone()
    return os.path.dirname(row[0]) if row else None


def _plan_backfill(con: sqlite3.Connection) -> tuple[list[tuple], list[tuple]]:
    """Returns (updates, missing).

    updates: ``(raw, enhanced, debug, slot1, slot2, slot3, id)`` rows ready for UPDATE — each
             value is the reconstructed path if that file exists on disk, else left unchanged
             (raw is guaranteed present; only raw gates inclusion).
    missing: ``(document_id, page_number, row_number)`` whose reconstructed crop is NOT on disk.
    """
    null_rows = con.execute(
        "SELECT id, document_id, page_number, row_number FROM vote_fields "
        "WHERE row_type='candidate' AND (raw_crop_path IS NULL OR raw_crop_path='')"
    ).fetchall()
    dir_cache: dict[str, str | None] = {}
    updates: list[tuple] = []
    missing: list[tuple] = []
    for vid, did, page, row in null_rows:
        if did not in dir_cache:
            dir_cache[did] = _sibling_crops_dir(con, did)
        crops_dir = dir_cache[did]
        if not crops_dir:
            missing.append((did, page, row))  # no sibling to anchor the path
            continue
        out_dir = os.path.dirname(crops_dir)  # crops dir's parent holds debug/ and slots/
        stem = f"{did}_p{page}_row{row}_candidate"
        raw = f"{crops_dir}/{stem}_field.png"
        if not os.path.exists(raw):
            missing.append((did, page, row))
            continue
        enhanced = f"{crops_dir}/{stem}_field_enhanced.png"
        debug = f"{out_dir}/debug/{stem}_debug.png"
        slots = [f"{out_dir}/slots/{stem}_slot{i}.png" for i in (1, 2, 3)]
        updates.append((
            raw,
            enhanced if os.path.exists(enhanced) else None,
            debug if os.path.exists(debug) else None,
            slots[0] if os.path.exists(slots[0]) else None,
            slots[1] if os.path.exists(slots[1]) else None,
            slots[2] if os.path.exists(slots[2]) else None,
            vid,
        ))
    return updates, missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path, help="path to results.sqlite to repair")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", action="store_true", default=True, help="report only (default)")
    grp.add_argument("--apply", action="store_true", help="write the fixes")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    apply = bool(args.apply)

    if not args.db.exists():
        print(f"refusing: DB not found: {args.db}", file=sys.stderr)
        return 2

    con = sqlite3.connect(args.db, timeout=60.0)
    try:
        updates, missing = _plan_backfill(con)
        surplus = [r[0] for r in con.execute(_SURPLUS_ROWS)]
        if not args.quiet:
            print(f"DB: {args.db}")
            print(f"  backfill: {len(updates)} crop path(s) recoverable from disk; "
                  f"{len(missing)} genuinely absent")
            print(f"  dedupe:   {len(surplus)} surplus row(s) to delete")
            if missing:
                for did, page, row in missing[:20]:
                    print(f"    ABSENT  {did} p{page} row{row}")
                if len(missing) > 20:
                    print(f"    … +{len(missing) - 20} more")

        if apply:
            con.execute("BEGIN")
            con.executemany(
                "UPDATE vote_fields SET raw_crop_path=?, "
                "enhanced_crop_path=COALESCE(?, enhanced_crop_path), "
                "debug_crop_path=COALESCE(?, debug_crop_path), "
                "slot_1_crop_path=COALESCE(?, slot_1_crop_path), "
                "slot_2_crop_path=COALESCE(?, slot_2_crop_path), "
                "slot_3_crop_path=COALESCE(?, slot_3_crop_path) WHERE id=?",
                updates,
            )
            if surplus:
                con.executemany("DELETE FROM vote_fields WHERE id=?", [(i,) for i in surplus])
            con.execute(_RECOMPUTE_N_CANDIDATES)
            con.commit()
            ensure_browse_indexes(args.db)  # rebuild/refresh /browse indexes + ANALYZE
            if not args.quiet:
                print(f"  applied: backfilled {len(updates)}, deleted {len(surplus)}, "
                      f"recomputed n_candidates, refreshed browse indexes")
        elif not args.quiet:
            print("  (dry-run — pass --apply to write; then run a crop publish + DB re-publish)")
    finally:
        con.close()

    return 3 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
