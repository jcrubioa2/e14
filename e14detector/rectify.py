"""Detect + rectify non-standard acta captures (photos / margined scans).

~1.6% of actas are not the dominant flatbed-scan geometry: they are phone photos
or re-scans of the *same* E-14 form at an arbitrary position, margin, rotation and
perspective (see ``scripts/acta_format_census.py``). The crop pipeline's fixed
normalized coordinates (``e14detector/layout.py``) only match the canonical
geometry, so on these the vote-column box lands off-target and the crop is
unreadable.

Rather than hand-measure a coordinate table per format (the form's position varies
continuously, so that can't work), we **detect the form and warp it back to the
canonical upright page**, after which the existing ``LAYOUT["r1"]`` coords apply
unchanged. A warped result that doesn't look like an E-14 (detection failed) is
rejected by ``sanity_score`` so the caller can quarantine it instead of emitting a
garbage crop.

This module is pure image-in/image-out (PIL + cv2 + numpy); it knows nothing about
the store, the round, or crop keys.

STATUS: EXPERIMENTAL — not wired into the processing pipeline. Rectification works on
most captures, but the ``sanity_score`` gate is not yet trustworthy enough to auto-publish:
consulado/embajador actas use a different printed layout than the domestic reference, so a
perfect rectification can still score low, and a single template can't cleanly separate
"good" from "mildly skewed". Until the gate uses per-form-type references (domestic /
consulado / embajador) or fiducial-corner registration, non-standard actas are instead
QUARANTINED (shown with their broken crops, voting disabled) — see
scripts/quarantine_nonnormal.py and e14detector/webapp.py. This module is the seed for the
deferred auto-recovery effort; scripts/rectify_validate.py exercises it.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# Canonical upright page proportions == the normal acta aspect (width/height ~0.3315).
# A rectified page is produced at this aspect so the normalized r1 coordinates land
# exactly where they do on a normal scan. Width is a working resolution (downstream
# rendering re-rasters from the warped image), not the final crop DPI.
CANON_ASPECT = 0.3315
CANON_W = 1000
CANON_H = round(CANON_W / CANON_ASPECT)  # ~3017

# A detected region must occupy a plausible fraction of the frame to be the form
# (rejects tiny specks and the whole-frame "everything is paper" non-detection).
_MIN_AREA_FRAC = 0.12
_MAX_AREA_FRAC = 0.985

# sanity_score >= this => we trust the rectification; below => quarantine.
SANITY_PASS = 0.55

# Reference ink profiles of a correctly-aligned canonical E-14, built from real normal
# actas by scripts/build_rectify_ref.py. The gate correlates a warp against these so a
# skewed warp (bands smeared) or a non-form page (barcode cover, fragment) scores low.
_REF_PATH = Path(__file__).with_name("rectify_ref.npz")


@lru_cache(maxsize=1)
def _reference() -> tuple[np.ndarray, np.ndarray] | None:
    if not _REF_PATH.exists():
        return None
    z = np.load(_REF_PATH)
    return z["row"].astype(np.float32), z["col"].astype(np.float32)


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation clamped to 0..1 (negative/degenerate -> 0)."""
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    if denom < 1e-9:
        return 0.0
    return max(0.0, float((a * b).sum() / denom))


@dataclass(frozen=True)
class RectifyResult:
    image: Image.Image | None  # rectified canonical page, or None if detection failed
    method: str                # which detector produced the quad
    score: float               # sanity_score of the warped page (0 if no image)

    @property
    def ok(self) -> bool:
        return self.image is not None and self.score >= SANITY_PASS


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as TL, TR, BR, BL (sum/diff trick)."""
    pts = pts.reshape(4, 2).astype("float32")
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).reshape(-1)
    return np.array([
        pts[np.argmin(s)],  # TL  (smallest x+y)
        pts[np.argmin(d)],  # TR  (smallest y-x)
        pts[np.argmax(s)],  # BR  (largest x+y)
        pts[np.argmax(d)],  # BL  (largest y-x)
    ], dtype="float32")


def _candidate_quads(gray: np.ndarray) -> list[tuple[np.ndarray, str]]:
    """Yield plausible (corners, method) for the form, best-effort, multiple strategies.

    Strategies are complementary: paper-vs-background contour handles photos of the
    sheet on a darker surface; the ink bounding box handles white-margin re-scans where
    the paper and the margin are the same colour. The caller warps each and keeps the
    one whose result scores best, so we never have to pick the right strategy up front.
    """
    h, w = gray.shape
    out: list[tuple[np.ndarray, str]] = []

    # Strategy 1+2: largest bright (paper) blob -> quad or rotated rectangle.
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _t, paper = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    paper = cv2.morphologyEx(paper, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    cnts, _ = cv2.findContours(paper, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if cnts:
        c = max(cnts, key=cv2.contourArea)
        frac = cv2.contourArea(c) / (h * w)
        if _MIN_AREA_FRAC < frac < _MAX_AREA_FRAC:
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            if len(approx) == 4:
                out.append((_order_corners(approx), f"paper-quad:{frac:.2f}"))
            out.append((_order_corners(cv2.boxPoints(cv2.minAreaRect(c))), f"paper-minarea:{frac:.2f}"))

    # Strategy 3: axis-aligned bounding box of dark ink (margined white scans).
    ink = (gray < 110).astype(np.uint8)
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    ys, xs = np.where(ink)
    if len(xs) > 500:
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        frac = (x1 - x0) * (y1 - y0) / (h * w)
        if _MIN_AREA_FRAC < frac < 1.0:
            corners = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype="float32")
            out.append((corners, f"ink-bbox:{frac:.2f}"))
    return out


def sanity_score(canonical_gray: np.ndarray) -> float:
    """0..1 confidence that a warped page is a *correctly-aligned* E-14.

    Primary signal is template correlation: the warp's row- and column-ink profiles vs a
    reference built from real normal actas (``_reference``). An aligned E-14 matches both
    (the header/section bands line up in y, the photo/logo/vote ink distribution lines up
    in x); a skewed warp smears the bands and a non-form page (barcode cover, fragment)
    has a wholly different profile — both score low and get quarantined.

    Two cheap structural gates act as a hard floor so a high spurious correlation can't
    pass a blank or inverted page: sane overall ink fill, and the left half (photos+logos)
    heavier than the right vote column. If the reference file is missing we fall back to
    those structural checks alone.
    """
    g = canonical_gray.astype(np.float32)
    h, w = g.shape
    ink = (g < 128).astype(np.float32)
    fill = float(ink.mean())
    if not (0.02 <= fill <= 0.55):
        return 0.0  # degenerate: blank page or black smear — never an acta

    left = float(ink[:, int(0.05 * w):int(0.55 * w)].mean())
    right = float(ink[:, int(0.69 * w):int(0.94 * w)].mean())
    left_heavy = 1.0 if left > right else 0.0  # photos+logos on the left, sparse votes right

    ref = _reference()
    if ref is None:
        # no template available — structural-only fallback
        row_ink = ink.mean(axis=1)
        band = row_ink[int(0.18 * h):int(0.42 * h)]
        has_band = band.size and float(band.max()) > 0.45
        return 0.5 * left_heavy + 0.5 * float(bool(has_band))

    row_ref, col_ref = ref
    row_ink = ink.mean(axis=1)
    col_ink = ink.mean(axis=0)
    if row_ink.shape != row_ref.shape:
        row_ink = np.interp(np.linspace(0, 1, row_ref.size), np.linspace(0, 1, row_ink.size), row_ink)
    if col_ink.shape != col_ref.shape:
        col_ink = np.interp(np.linspace(0, 1, col_ref.size), np.linspace(0, 1, col_ink.size), col_ink)
    # template correlation (alignment) is the spine; left-heavy structure is a small bonus
    return 0.55 * _corr(row_ink, row_ref) + 0.25 * _corr(col_ink, col_ref) + 0.20 * left_heavy


def rectify_image(pil_img: Image.Image) -> RectifyResult:
    """Detect the form and warp it to the canonical upright page.

    Tries each candidate quad, warps, scores, and returns the best. ``RectifyResult.ok``
    is True only when the best warp clears ``SANITY_PASS``; otherwise the caller should
    quarantine the acta (show the broken crop, disable voting) rather than trust it.
    """
    gray_full = np.array(pil_img.convert("L"))
    rgb = np.array(pil_img.convert("RGB"))
    dst = np.array([[0, 0], [CANON_W, 0], [CANON_W, CANON_H], [0, CANON_H]], dtype="float32")

    best: RectifyResult = RectifyResult(image=None, method="no-detection", score=0.0)
    for corners, method in _candidate_quads(gray_full):
        try:
            M = cv2.getPerspectiveTransform(corners, dst)
            warped = cv2.warpPerspective(rgb, M, (CANON_W, CANON_H))
        except cv2.error:
            continue
        score = sanity_score(cv2.cvtColor(warped, cv2.COLOR_RGB2GRAY))
        if score > best.score:
            best = RectifyResult(image=Image.fromarray(warped), method=method, score=score)
    return best
