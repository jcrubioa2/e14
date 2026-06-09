#!/usr/bin/env python3
"""PROTOTYPE: OCR-anchor rectification of non-standard acta photos.

Idea (user-directed): the E-14 form has fixed PRINTED text — the candidate surnames sit at
known y-fractions of the page (CEPEDA 0.417, CLAUDIA LOPEZ 0.503 ... SONDRA 0.900). So instead
of trusting a fuzzy ink-correlation gate, we:

  1. generate candidate page warps (reuse e14detector.rectify._candidate_quads),
  2. OCR each warp (easyocr, CPU),
  3. pick the warp where the most printed surnames land near their expected rows,
  4. refit the vertical alignment from those matched anchors (1-D least squares),
  5. crop with the existing r1 coordinates on the corrected page.

Confidence is principled: how many of the 7 printed names were found at the right place, and
the residual of the y-fit. This both (a) recovers good warps the old gate wrongly rejected and
(b) rejects misaligned warps the old gate happened to pass.

This is a MEASUREMENT prototype — it reports a recovery rate on a sample and writes a
before/after gallery for eyeballing. It does NOT touch the store, crops bucket, or pipeline.

    .venv/bin/python scripts/ocr_rectify_proto.py --limit 40
    .venv/bin/python scripts/ocr_rectify_proto.py --ids 88_250_010_02_003 28_240_000_00_036
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import unicodedata
from pathlib import Path

import cv2
import fitz
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14detector import layout  # noqa: E402
from e14detector.rectify import CANON_H, CANON_W, _candidate_quads, _order_corners  # noqa: E402

DATA = ROOT / "data"
CENSUS = DATA / "format_census" / "manifest.json"
QLIST = DATA / "format_census" / "quarantine_list.txt"
OUT = DATA / "format_census" / "ocr_rectify_proto"

# Canonical normalized (x, y) of each printed-text fiducial, built from real normal actas by
# the canon_anchors builder. These span the page in BOTH axes (CANDIDATO/AGRUPACION/VOTACION
# headers across the top, the 7 candidate surnames down the left) so a homography from matched
# anchors fully corrects rotation, scale AND horizontal perspective — not just vertical offset.
_ANCHOR_PATH = DATA / "format_census" / "canon_anchors.json"
CANON_ANCHORS = {k: (v["x"], v["y"]) for k, v in json.loads(_ANCHOR_PATH.read_text()).items()}

MIN_INLIERS = 6       # need >=6 printed fiducials agreeing on one homography to trust the page
MAX_RESIDUAL = 0.012  # median reprojection error (fraction of page) of the inlier fiducials


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    return "".join(c for c in s if c.isalpha() or c == " ")


def _render_page1(path: Path, max_px: int = 1700) -> Image.Image:
    doc = fitz.open(path)
    pg = doc.load_page(0)
    z = min(2.0, max_px / max(pg.rect.width, pg.rect.height))
    pix = pg.get_pixmap(matrix=fitz.Matrix(z, z))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()
    return img


def _warps(img: Image.Image) -> list[tuple[np.ndarray, str]]:
    """Candidate canonical-page warps (RGB arrays) + method label."""
    gray = np.array(img.convert("L"))
    rgb = np.array(img.convert("RGB"))
    dst = np.array([[0, 0], [CANON_W, 0], [CANON_W, CANON_H], [0, CANON_H]], dtype="float32")
    out = []
    for corners, method in _candidate_quads(gray):
        try:
            M = cv2.getPerspectiveTransform(corners.astype("float32"), dst)
            warped = cv2.warpPerspective(rgb, M, (CANON_W, CANON_H))
        except cv2.error:
            continue
        out.append((warped, method))
    return out


def _ocr_match(reader, warp_rgb: np.ndarray) -> list[tuple[str, float, float, float, float]]:
    """OCR the warp; return (keyword, canon_x, canon_y, det_x, det_y) for each fiducial found.

    Deduped per keyword by OCR confidence. Detected coords are normalized to the warp size.
    """
    h, w = warp_rgb.shape[:2]
    small = cv2.resize(warp_rgb, (int(w * 0.6), int(h * 0.6)))
    found: dict[str, tuple[float, float, float]] = {}  # kw -> (conf, det_x, det_y)
    for box, text, conf in reader.readtext(small, detail=1, paragraph=False):
        t = _norm(text)
        for kw in CANON_ANCHORS:
            if kw in t:
                xs = [p[0] for p in box]; ys = [p[1] for p in box]
                dx = (min(xs) + max(xs)) / 2 / small.shape[1]
                dy = (min(ys) + max(ys)) / 2 / small.shape[0]
                if kw not in found or conf > found[kw][0]:
                    found[kw] = (conf, dx, dy)
    return [(kw, *CANON_ANCHORS[kw][:1], CANON_ANCHORS[kw][1], dx, dy)
            for kw, (_c, dx, dy) in found.items()]


def _register(matches: list[tuple[str, float, float, float, float]]):
    """Fit a homography canonical->warp from fiducial matches, guarding against extrapolation.

    Returns (H, n_inliers, median_residual). H maps a canonical normalized point to the warp.
    The vote column (canonical x~0.69-0.94) must be *interpolated*: we require the VOTACION
    header among the inliers and a real left-to-right inlier span, else the placement would be
    an unconstrained extrapolation (the C-type false positive) and we report it unusable.
    """
    if len(matches) < 4:
        return None, len(matches), 1.0
    kws = [m[0] for m in matches]
    src = np.array([[m[1], m[2]] for m in matches], dtype="float32")
    dst = np.array([[m[3], m[4]] for m in matches], dtype="float32")
    # Full affine (6-DOF), not homography: with no bottom-right fiducial a homography's
    # perspective terms swing the unconstrained corner and the lowest vote rows drift. Affine
    # has no perspective freedom, so the bottom stays pinned by the SONDRA/lower-name inliers.
    M, mask = cv2.estimateAffine2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=0.02)
    if M is None:
        return None, 0, 1.0
    H = np.vstack([M, [0.0, 0.0, 1.0]]).astype("float64")
    inl = mask.ravel().astype(bool)
    n = int(inl.sum())
    if n == 0:
        return None, 0, 1.0
    inl_kws = {kws[i] for i in range(len(kws)) if inl[i]}
    inl_x = src[inl][:, 0]
    # vote column must be bracketed by fiducials, not extrapolated from a left-only cluster
    if "VOTACION" not in inl_kws or inl_x.min() > 0.30 or inl_x.max() < 0.80:
        return None, n, 1.0
    proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), H).reshape(-1, 2)
    resid = np.linalg.norm(proj[inl] - dst[inl], axis=1)
    return H, n, float(np.median(resid))


def _overlay_H(warp_rgb: np.ndarray, H: np.ndarray) -> Image.Image:
    """Draw the r1 vote-slot boxes mapped through the homography H (canonical->warp)."""
    im = Image.fromarray(warp_rgb).copy()
    d = ImageDraw.Draw(im)
    Hh, W = warp_rgb.shape[:2]
    corners = []
    for f in layout.field_layouts_for_page(1, CANON_W, CANON_H):
        for box in f.slot_boxes:
            corners.append([[box.x0 / CANON_W, box.y0 / CANON_H],
                            [box.x1 / CANON_W, box.y0 / CANON_H],
                            [box.x1 / CANON_W, box.y1 / CANON_H],
                            [box.x0 / CANON_W, box.y1 / CANON_H]])
    pts = np.array(corners, dtype="float32").reshape(-1, 1, 2)
    proj = cv2.perspectiveTransform(pts, H).reshape(-1, 4, 2)
    for quad in proj:
        poly = [(float(x) * W, float(y) * Hh) for x, y in quad]
        d.polygon(poly, outline=(255, 0, 0), width=4)
    return im


def _orient(reader, img: Image.Image) -> tuple[Image.Image, int, int]:
    """Coarse 90deg orientation by OCR fiducial vote -> (upright_img, k_degrees, n_fiducials).

    easyocr (like any OCR) only reads horizontal text, so a sideways/upside-down consulado photo
    yields nothing and gets wrongly rejected. We OCR the page at 0/90/180/270 and keep the turn
    that surfaces the most printed fiducials. Short-circuits at 0deg for the common upright case
    so we don't pay 4x OCR on pages that are already the right way up. (Fine skew + perspective
    are then corrected by the fiducial affine fit downstream.)
    """
    turns = [(0, img), (90, img.rotate(-90, expand=True)),
             (180, img.rotate(180, expand=True)), (270, img.rotate(90, expand=True))]
    best = (0, -1, img)  # (k, n, image)
    for k, rimg in turns:
        arr = np.array(rimg.convert("RGB"))
        s = min(1.0, 1600 / max(arr.shape[:2]))
        if s < 1.0:
            arr = cv2.resize(arr, (int(arr.shape[1] * s), int(arr.shape[0] * s)))
        n = len(_ocr_match(reader, arr))
        if n > best[1]:
            best = (k, n, rimg)
        if k == 0 and n >= 5:
            break
    return best[2], best[0], best[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--ids", nargs="*", default=None, help="specific doc id fragments")
    args = ap.parse_args()

    recs = {r["document_id"]: r for r in json.loads(CENSUS.read_text())}
    ql = [l.strip() for l in QLIST.read_text().splitlines() if l.strip()]
    if args.ids:
        todo = [recs[d] for d in recs if any(frag in d for frag in args.ids)]
    else:
        pool = [recs[d] for d in ql if d in recs]
        pool.sort(key=lambda r: r["document_id"])
        # even spread across the list
        step = max(1, len(pool) // args.limit)
        todo = pool[::step][: args.limit]

    print("loading easyocr (CPU, es)...")
    import easyocr
    reader = easyocr.Reader(["es"], gpu=False, verbose=False)

    OUT.mkdir(parents=True, exist_ok=True)
    cards = []
    recovered = 0
    for i, rec in enumerate(todo, 1):
        p = Path(rec["path"])
        if not p.is_absolute():
            p = ROOT / p
        img = _render_page1(p)
        img, kdeg, kn = _orient(reader, img)  # turn the page upright before anything else
        # Candidates: the raw page resized to canonical (the affine fit absorbs moderate skew)
        # PLUS each quad warp (helps when the sheet sits on a dark/cluttered background). OCR+
        # register each; keep the best-supported fit, but stop early once one clears the gate.
        raw_canon = cv2.resize(np.array(img.convert("RGB")), (CANON_W, CANON_H))
        candidates = [(raw_canon, f"raw@{kdeg}")] + _warps(img)
        best = None  # (key, n_inliers, resid, warp, H, method)
        for warp, method in candidates:
            matches = _ocr_match(reader, warp)
            H, n, resid = _register(matches)
            if H is None:
                continue
            key = (n, -resid)
            if best is None or key > best[0]:
                best = (key, n, resid, warp, H, method)
            if n >= MIN_INLIERS and resid <= MAX_RESIDUAL:
                break  # good enough; don't pay for more OCR passes
        if best is None:
            status, n, resid, method = "no-anchors", 0, 1.0, "-"
            thumb = img.copy(); thumb.thumbnail((360, 1100))
        else:
            _key, n, resid, warp, H, method = best
            ok = n >= MIN_INLIERS and resid <= MAX_RESIDUAL
            status = "RECOVERED" if ok else "reject"
            recovered += int(ok)
            thumb = _overlay_H(warp, H); thumb.thumbnail((360, 1100))
        # failure-mode label for the funnel: distinguish "OCR saw nothing" (dark/illegible) from
        # "OCR read it but geometry/extrapolation failed" (low-fid / partial).
        if status == "RECOVERED":
            mode = "recovered"
        elif kn == 0:
            mode = "illegible"      # no fiducials at any rotation -> too dark/blurred/cropped
        elif kn < 4:
            mode = "low-ocr"        # a few fiducials, not enough to register
        else:
            mode = "geometry"       # enough text but the fit/guard rejected the placement
        bio = OUT / f"{rec['document_id']}.png"
        thumb.save(bio)
        cards.append({"id": rec["document_id"], "dept": rec["document_id"].split("_")[2],
                      "wh": f"{int(rec['w'])}x{int(rec['h'])}", "status": status, "mode": mode,
                      "n": n, "resid": round(resid, 4), "kdeg": kdeg, "kn": kn, "file": bio.name})
        print(f"[{i}/{len(todo)}] {rec['document_id']:38s} {status:10s} inliers={n} resid={resid:.4f} "
              f"rot={kdeg} fid0={kn} mode={mode} via={method}")

    # gallery
    def tile(c):
        col = {"RECOVERED": "#2ea043", "reject": "#d2691e", "no-anchors": "#999"}[c["status"]]
        return (f'<a class=t href="{c["file"]}" target=_blank>'
                f'<img loading=lazy src="{c["file"]}">'
                f'<span style="color:{col}">{c["status"]} · {c["n"]} fid · r={c["resid"]}</span>'
                f'<small>dept {c["dept"]} · {c["wh"]}<br>{c["id"]}</small></a>')
    html = f"""<!doctype html><meta charset=utf-8><title>OCR rectify proto</title>
<style>body{{font:13px system-ui;background:#0f1115;color:#e6e6e6;margin:0}}
header{{padding:14px 18px;background:#171a21;position:sticky;top:0;border-bottom:1px solid #262b36}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;padding:16px}}
.t{{background:#1b1f27;border:1px solid #2a3140;border-radius:8px;overflow:hidden;text-decoration:none;color:#cbd3df}}
.t img{{width:100%;height:340px;object-fit:contain;background:#000;display:block}}
.t span{{display:block;padding:5px 8px;font-weight:600}} .t small{{display:block;padding:0 8px 8px;color:#8b94a3}}</style>
<header><b>OCR-anchor rectification — {recovered}/{len(cards)} recovered</b>
 ({MIN_INLIERS}+ fiducials on one homography, residual &le; {MAX_RESIDUAL}). Red boxes = where votes would be cropped.</header>
<div class=grid>{''.join(tile(c) for c in cards)}</div>"""
    (OUT / "index.html").write_text(html, encoding="utf-8")

    # funnel breakdown (feeds the /transparencia funnel + count-model doc)
    from collections import Counter
    modes = Counter(c["mode"] for c in cards)
    rots = Counter(c["kdeg"] for c in cards if c["status"] == "RECOVERED")
    summary = {"sample": len(cards), "recovered": recovered,
               "rate": round(recovered / max(1, len(cards)), 3),
               "modes": dict(modes), "recovered_by_rotation": dict(rots)}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"\n=== {recovered}/{len(cards)} recovered ({100*recovered/max(1,len(cards)):.0f}%) ===")
    print("modes:", dict(modes))
    print("recovered_by_rotation:", dict(rots))
    print(f"gallery: {OUT/'index.html'}")
    print(f'  explorer.exe "$(wslpath -w {OUT/"index.html"})"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
