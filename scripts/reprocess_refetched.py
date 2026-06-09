#!/usr/bin/env python3
"""Reprocess the re-fetched actas and reconcile their quarantine state.

After scripts/refetch_actas.py pulls fresh copies, this re-censuses each non-normal acta's
CURRENT geometry, reprocesses it through the detector (force), and reconciles:

  * now NORMAL geometry  -> good crops; clear quarantine; reset its (stale) votes.
  * still non-standard    -> keep quarantined (still unreadable).

By default it only touches actas already present in the results DB (the served corpus), so
re-fetching never silently grows the corpus. Run AFTER a re-fetch; safe to run on stale files
too (everything just stays quarantined). Crop re-upload + publish are separate steps.

    .venv/bin/python scripts/reprocess_refetched.py --dry-run
    .venv/bin/python scripts/reprocess_refetched.py \
        --results-db data/detector_national/results/results.sqlite \
        --output-dir data/detector_national \
        --community-db data/detector_national/community.sqlite
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sqlite3
import sys
from pathlib import Path

# MEMORY SAFETY: heavy 4K consulado PDFs have OOM-crashed the WSL VM during cropping. Cap the
# rendered bitmap budget low (the digit crops don't need a huge raster) BEFORE pdf_render reads
# it, and reprocess strictly sequentially. We also only crop actas that re-fetched to NORMAL
# geometry (small clean scans); still-heavy/non-standard ones are kept quarantined and NEVER
# rendered here. Override with E14_MAX_RENDER_MP if needed.
os.environ.setdefault("E14_MAX_RENDER_MP", "24")

# Absolute backstop: a re-fetched normal scan is ~100 KB. Only a pathological file would exceed
# this; skip cropping it (stay quarantined) so it can't OOM a worker. The render-MP cap above is
# the primary guard — it auto-downscales any oversized page so even big normal scans are bounded.
MAX_CROP_BYTES = 80 * 1024 * 1024

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from concurrent.futures import ProcessPoolExecutor  # noqa: E402

from e14detector.community import make_store  # noqa: E402
from e14detector.processor import _max_inflight, _run_pool_bounded  # noqa: E402
from e14detector.storage import DetectorStore  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from acta_format_census import classify  # noqa: E402  (reuse the geometry classifier)

DATA = ROOT / "data"
CENSUS = DATA / "format_census" / "manifest.json"


def fixability(pdf_path: Path) -> str:
    """Classify a (re-fetched) PDF's page-1 geometry into how we can crop it:

      'normal'  -> canonical scan, normal coords
      'anchored'-> clean wide/other geometry recoverable via a per-format anchor
      'hard'    -> photo / long-tail geometry we can't reliably crop -> quarantine
      'error'   -> unreadable

    Delegates to layout.geometry_disposition so this script and the from-scratch processor
    apply the SAME recover/flag decision (quarantine -> 'hard' here).
    """
    import fitz

    from e14detector.layout import geometry_disposition

    try:
        d = fitz.open(pdf_path)
        try:
            r = d.load_page(0).rect
            w, h = round(r.width, 1), round(r.height, 1)
        finally:
            d.close()
    except Exception:  # noqa: BLE001
        return "error"
    return {"normal": "normal", "anchored": "anchored", "quarantine": "hard"}[
        geometry_disposition(int(w), int(h))
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--census", type=Path, default=CENSUS)
    ap.add_argument("--results-db", type=Path, default=DATA / "detector_national" / "results" / "results.sqlite")
    ap.add_argument("--output-dir", type=Path, default=DATA / "detector_national")
    ap.add_argument("--community-db", type=Path, default=None)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--workers", type=int, default=min((os.cpu_count() or 4), 10),
                    help="parallel crop workers (memory ~= workers * render-mp)")
    ap.add_argument("--max-render-mp", type=float, default=24.0,
                    help="per-page megapixel cap; auto-downscales heavy pages so workers can't OOM")
    ap.add_argument("--all", action="store_true", help="also process docs not yet in the results DB")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # Set BEFORE the pool forks so every worker inherits the cap (the primary OOM guard).
    os.environ["E14_MAX_RENDER_MP"] = str(args.max_render_mp)

    recs = [r for r in json.loads(args.census.read_text()) if r["format"] in ("wide", "other")]

    # restrict to docs already in the served corpus unless --all
    present: set[str] = set()
    con = sqlite3.connect(args.results_db)
    for (did,) in con.execute("SELECT document_id FROM documents"):
        present.add(did)
    con.close()
    if not args.all:
        recs = [r for r in recs if r["document_id"] in present]
    print(f"actas to reprocess: {len(recs)}")

    # 1. re-census each (post-refetch). NORMAL scans + clean wide/other geometries (anchored)
    #    get cropped; photos / long-tail stay quarantined and are NEVER rendered (memory safety).
    crop: list[tuple[str, Path]] = []   # (doc_id, src) — fixable (normal or anchored)
    stays: list[str] = []               # hard / missing / pathological -> quarantined
    n_normal = n_anchored = skipped_huge = 0
    for r in recs:
        src = Path(r["path"]);  src = src if src.is_absolute() else ROOT / src
        fx = fixability(src) if src.exists() else "missing"
        if fx in ("normal", "anchored") and src.stat().st_size <= MAX_CROP_BYTES:
            crop.append((r["document_id"], src))
            n_normal += fx == "normal"
            n_anchored += fx == "anchored"
        else:
            if fx in ("normal", "anchored"):
                skipped_huge += 1  # pathologically large; skip to be safe, stay quarantined
            stays.append(r["document_id"])
    print(f"  fixable -> will re-crop: {len(crop)}  ({n_normal} normal, {n_anchored} anchored wide/other)")
    print(f"  hard / missing -> stay quarantined: {len(stays)}  (incl. {skipped_huge} fixable-but-huge)")
    print(f"  crop pool: {args.workers} workers, render cap {args.max_render_mp} MP/page "
          f"(~{args.workers * args.max_render_mp * 3 / 1024:.1f} GB raster ceiling)")

    if args.dry_run:
        print("(dry run) no reprocess, no DB writes")
        return 0

    # 2. reprocess the now-normal actas in PARALLEL. Memory is bounded two ways: the render-MP cap
    #    downscales any heavy page (so per-worker raster <= cap), and _run_pool_bounded keeps only a
    #    small window of jobs in flight. Crops are small clean scans, so this is the safe set to
    #    parallelize; the heavy still-non-standard files are never rendered.
    store = DetectorStore(args.results_db, args.output_dir / "results" / "results.jsonl")
    totals = {"done": 0, "skipped": 0, "failed": 0, "fields": 0}
    todo = [src for _, src in crop]
    if todo:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            _run_pool_bounded(ex, todo, args.output_dir, store, args.dpi, False, False,
                              totals, _max_inflight(args.workers))
    gc.collect()

    # 3. reconcile quarantine: clear for the re-cropped normals, keep for the rest
    fixed = [doc_id for doc_id, _ in crop]
    store.set_quarantined(fixed, value=0)
    store.set_quarantined(stays, value=1)
    store.commit()
    store.close()
    print(f"reprocessed done={totals['done']} failed={totals['failed']} fields={totals['fields']}; "
          f"un-quarantined {len(fixed)}, kept {len(stays)} quarantined")

    # 4. votes on the now-fixed actas were cast on old crops -> reset them
    if fixed:
        community = make_store(args.community_db)
        vote_totals = {"flags": 0, "appeals": 0, "field_state": 0, "cid_index": 0}
        try:
            for did in fixed:
                for k, v in community.delete_document(did).items():
                    vote_totals[k] = vote_totals.get(k, 0) + v
        finally:
            community.close()
        print(f"votes reset on fixed actas: {vote_totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
