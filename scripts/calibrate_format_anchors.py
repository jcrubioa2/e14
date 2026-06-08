#!/usr/bin/env python3
"""Calibrate the per-format crop anchors in e14detector/layout.py (FORMAT_ANCHORS).

The clean wide/other scan clusters are the SAME E-14 form inside a page sub-rectangle. We map
the r1 normalized coords into that rectangle via an affine anchor. The vertical fit matters
(a constant offset clips the digits), so we fit it against the *measured* printed candidate-cell
boundaries detected in the votación column over a sample of each cluster; x comes from the form's
ink extent. Prints the NormalizedBox to paste into FORMAT_ANCHORS.

    .venv/bin/python scripts/calibrate_format_anchors.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import fitz
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14detector.pdf_render import render_pdf_pages  # noqa: E402

CENSUS = ROOT / "data" / "format_census" / "manifest.json"
# r1 candidate-cell boundaries (page-relative on a normal scan); 8 edges for 7 cells.
R1_EDGES = np.array([0.384, 0.475, 0.554, 0.633, 0.713, 0.791, 0.872, 0.944])
# x anchor (form ink extent) is stable per cluster; reused from the census ink-bbox calibration.
X_ANCHOR = {"wide": (0.2395, 0.7544), "other": (0.2403, 0.7655)}
# (cluster, aspect-lo, aspect-hi, votación-column x-range, candidate-region y-range, n cell edges)
# y-range brackets the candidate cells for that cluster (wide's top edge is ~0.33; other's form is
# taller so candidate 7 sits near/below the page bottom and its boundaries run higher).
CLUSTERS = [
    ("wide", 0.58, 0.64, (0.60, 0.73), (0.30, 0.92), 8),
    ("other", 0.66, 0.74, (0.62, 0.77), (0.40, 0.99), 7),
]


def _dedupe(cents: list[float], tol: float) -> list[float]:
    out: list[float] = []
    for c in sorted(cents):
        if out and c - out[-1] < tol:
            out[-1] = (out[-1] + c) / 2
        else:
            out.append(c)
    return out


def cell_edges(pdf: Path, xr: tuple[float, float], yr: tuple[float, float]) -> list[float]:
    """Page-y of the printed separator lines in the votación column (candidate region)."""
    pg = render_pdf_pages(pdf, pages=(1,), dpi=150)[0]
    a = np.array(pg.image.convert("L"))
    h, w = a.shape
    rowfrac = (a[:, int(xr[0] * w):int(xr[1] * w)] < 110).mean(axis=1)
    runs: list[list[float]] = []
    for y in (y for y in range(h) if rowfrac[y] > 0.45):
        yf = y / h
        if runs and yf - runs[-1][-1] <= 0.006:
            runs[-1].append(yf)
        else:
            runs.append([yf])
    cents = _dedupe([sum(r) / len(r) for r in runs], 0.04)
    return [c for c in cents if yr[0] < c < yr[1]]


def sample_paths(lo: float, hi: float, n: int) -> list[Path]:
    out = []
    for r in json.loads(CENSUS.read_text()):
        if r["format"] not in ("wide", "other"):
            continue
        p = Path(r["path"])
        if not p.exists():
            continue
        try:
            ar = (lambda rect: rect.width / rect.height)(fitz.open(p).load_page(0).rect)
        except Exception:  # noqa: BLE001
            continue
        if lo <= ar < hi:
            out.append(p)
        if len(out) >= n:
            break
    return out


def main() -> int:
    for name, lo, hi, xr, yr, n_edges in CLUSTERS:
        rows = []
        for p in sample_paths(lo, hi, 14):
            e = cell_edges(p, xr, yr)
            if len(e) >= n_edges:
                rows.append(e[:n_edges])
        if not rows:
            print(f"{name}: no clean samples (adjust xr / threshold)")
            continue
        med = np.median(np.array(rows), axis=0)
        r1 = R1_EDGES[:n_edges]
        a0, b = np.linalg.lstsq(np.vstack([np.ones(n_edges), r1]).T, med, rcond=None)[0]
        resid = float(np.max(np.abs((a0 + b * r1) - med)))
        x0, x1 = X_ANCHOR[name]
        print(f'"{name}": NormalizedBox({x0}, {a0:.4f}, {x1}, {a0 + b:.4f}),  '
              f"# n={len(rows)} maxresid={resid:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
