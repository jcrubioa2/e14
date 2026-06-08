#!/usr/bin/env python3
"""Build the canonical E-14 reference ink profiles used by e14detector/rectify.py.

The rectification sanity gate correlates each warped page against the row/column ink
profile of a correctly-aligned normal acta. This builds that reference (median over a
sample of real normal actas, page 1, at canonical size) and writes it next to the
module as ``e14detector/rectify_ref.npz``.

    .venv/bin/python scripts/build_rectify_ref.py --n 30
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14detector.pdf_render import render_pdf_pages  # noqa: E402
from e14detector.rectify import CANON_H, CANON_W, _REF_PATH  # noqa: E402

CENSUS = ROOT / "data" / "format_census" / "manifest.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--census", type=Path, default=CENSUS)
    ap.add_argument("--n", type=int, default=30, help="normal actas to median over")
    args = ap.parse_args()

    normals = [r for r in json.loads(args.census.read_text()) if r["format"] == "normal"]
    rows, cols, used = [], [], 0
    for r in normals:
        try:
            pg = render_pdf_pages(Path(r["path"]), pages=(1,), dpi=120)[0]
            ink = (np.array(pg.image.convert("L").resize((CANON_W, CANON_H))) < 128).astype(np.float32)
        except Exception:  # noqa: BLE001
            continue
        rows.append(ink.mean(axis=1))
        cols.append(ink.mean(axis=0))
        used += 1
        if used >= args.n:
            break

    row_ref = np.median(np.array(rows), axis=0).astype(np.float32)
    col_ref = np.median(np.array(cols), axis=0).astype(np.float32)
    np.savez(_REF_PATH, row=row_ref, col=col_ref, n=used)
    print(f"built reference from {used} normal actas -> {_REF_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
