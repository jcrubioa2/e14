#!/usr/bin/env python3
"""Build the canonical printed-text anchor map used by e14detector/ocr_rectify.py.

For each E-14 page we OCR a handful of *normal* (canonical-geometry) actas and record where each
printed fiducial lands — the column headers (CANDIDATO / AGRUPACION / VOTACION) and the printed
candidate surnames for that page. These canonical positions are what a non-standard photo's OCR
output is registered against (affine fit) so the normal crop coordinates can be reused.

Writes data/format_census/canon_anchors.json = {"1": {KW: {x,y,n}}, "2": {...}}.

    .venv/bin/python scripts/build_canon_anchors.py --samples 10
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

import cv2
import fitz
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14detector import layout  # noqa: E402
from e14detector.rectify import CANON_H, CANON_W  # noqa: E402

DATA = ROOT / "data"
CENSUS = DATA / "format_census" / "manifest.json"
OUT = DATA / "format_census" / "canon_anchors.json"

HEADERS = {"CANDIDATO", "AGRUPACION", "VOTACION"}
# Distinctive surname token per candidate (avoids first-name collisions), keyed by row number.
SURNAME = {
    1: "CEPEDA", 2: "LOPEZ", 3: "BOTERO", 4: "ESPRIELLA", 5: "LIZCANO", 6: "URIBE",
    7: "MACOLLINS", 8: "BARRERAS", 9: "CAICEDO", 10: "MATAMOROS", 11: "VALENCIA",
    12: "FAJARDO", 13: "MURILLO",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    return "".join(c for c in s if c.isalpha() or c == " ")


def _page_keywords(page_number: int) -> set[str]:
    kws = set(HEADERS)
    for f in layout.field_layouts_for_page(page_number, 1000, 3017):
        if f.row.row_type == "candidate":
            kws.add(SURNAME[f.row.row_number])
    return kws


def _render_canon(pdf: Path, page_number: int) -> np.ndarray | None:
    d = fitz.open(pdf)
    try:
        if page_number - 1 >= d.page_count:
            return None
        pg = d.load_page(page_number - 1)
        z = min(2.0, 1700 / max(pg.rect.width, pg.rect.height))
        pix = pg.get_pixmap(matrix=fitz.Matrix(z, z))
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
        return cv2.resize(arr, (CANON_W, CANON_H))
    finally:
        d.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", type=int, default=10)
    args = ap.parse_args()

    recs = [r for r in json.loads(CENSUS.read_text()) if r["format"] == "normal"]
    import random
    random.seed(7)
    paths = [Path(r["path"]) for r in random.sample(recs, args.samples)]
    paths = [p if p.is_absolute() else ROOT / p for p in paths]

    import easyocr
    reader = easyocr.Reader(["es"], gpu=False, verbose=False)

    out: dict[str, dict] = {}
    for page in (1, 2):
        kws = _page_keywords(page)
        acc: dict[str, list[tuple[float, float]]] = {k: [] for k in kws}
        for p in paths:
            arr = _render_canon(p, page)
            if arr is None:
                continue
            for box, text, conf in reader.readtext(arr, detail=1, paragraph=False):
                t = _norm(text)
                xs = [q[0] for q in box]; ys = [q[1] for q in box]
                xc = (min(xs) + max(xs)) / 2 / CANON_W
                yc = (min(ys) + max(ys)) / 2 / CANON_H
                for k in kws:
                    if k in t:
                        # headers can also appear in the page title; keep only the top-band hit
                        if k in HEADERS and not (0.12 <= yc <= 0.42):
                            continue
                        acc[k].append((xc, yc))
        out[str(page)] = {
            k: {"x": round(float(np.median(np.array(v)[:, 0])), 4),
                "y": round(float(np.median(np.array(v)[:, 1])), 4), "n": len(v)}
            for k, v in acc.items() if v
        }
        print(f"page {page}: {len(out[str(page)])} fiducials")
        for k, val in sorted(out[str(page)].items(), key=lambda kv: kv[1]["y"]):
            print(f"  {k:12s} x={val['x']:.3f} y={val['y']:.3f} n={val['n']}")

    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
