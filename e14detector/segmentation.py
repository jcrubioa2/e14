"""Adaptive slot segmentation for vote fields.

Handwritten 3-digit counts drift left/right inside the printed box, so fixed
equal-thirds boundaries can slice a single digit in half. Instead we keep the
3-slot model but place the two internal cut lines at the lowest-ink columns
(the gaps between digits), searching only within a window around each nominal
third so a missing gap (touching digits) safely falls back near the fixed
position. Outer edges and top/bottom are inset to drop printed borders.
"""
from __future__ import annotations

import numpy as np

from .layout import PixelBox

SLOT_INSET_X = 0.06
SLOT_INSET_Y = 0.04
# How far (fraction of the inner width) each internal cut may move from its
# nominal third while hunting for an ink valley.
VALLEY_WINDOW = 0.12
# Minimum slot width as a fraction of inner width, to avoid degenerate slots.
MIN_SLOT_FRAC = 0.12


def _valley_cut(col_ink: np.ndarray, lo: int, hi: int, nominal: float) -> int:
    """Cut at the centre of the low-ink gap within [lo, hi).

    Using the gap centre (rather than the first minimum column) keeps the two
    slots balanced in width, so the ratio-based component thresholds, which were
    tuned for ~equal-width slots, stay valid.
    """
    if hi <= lo:
        return int(round(nominal))
    segment = col_ink[lo:hi]
    low = float(segment.min())
    high = float(segment.max())
    tolerance = low + 0.15 * (high - low)
    near_min = np.where(segment <= tolerance)[0]
    return lo + int(round(float(near_min.mean())))


def _internal_cuts(col_ink: np.ndarray, left: int, right: int) -> tuple[int, int]:
    inner = max(1, right - left)
    cuts: list[int] = []
    prev = left
    for k in (1, 2):
        nominal = left + inner * k / 3.0
        window = inner * VALLEY_WINDOW
        lo = int(max(prev + inner * MIN_SLOT_FRAC, nominal - window))
        hi = int(min(right - inner * MIN_SLOT_FRAC, nominal + window))
        cut = _valley_cut(col_ink, lo, hi, nominal)
        cuts.append(cut)
        prev = cut
    return cuts[0], cuts[1]


def adaptive_slot_boxes(field_box: PixelBox, field_binary: np.ndarray) -> tuple[PixelBox, PixelBox, PixelBox]:
    """Return three page-space slot boxes for a field, snapped to ink valleys.

    ``field_binary`` is the preprocessed (ink=non-zero) field crop whose pixel
    columns align with ``field_box`` (local x 0 == field_box.x0).
    """
    height, width = field_binary.shape
    col_ink = (field_binary > 0).sum(axis=0).astype(np.float64)
    inset_x = int(round(width * SLOT_INSET_X))
    inset_y = int(round(height * SLOT_INSET_Y))
    left, right = inset_x, max(inset_x + 1, width - inset_x)
    c1, c2 = _internal_cuts(col_ink, left, right)

    x_edges = [left, c1, c2, right]
    boxes: list[PixelBox] = []
    for a, b in zip(x_edges[:-1], x_edges[1:]):
        boxes.append(
            PixelBox(
                x0=field_box.x0 + a,
                y0=field_box.y0 + inset_y,
                x1=field_box.x0 + b,
                y1=field_box.y0 + (height - inset_y),
            ).normalized()
        )
    return boxes[0], boxes[1], boxes[2]


def local_slot_bounds(field_binary: np.ndarray, slot_boxes: tuple[PixelBox, PixelBox, PixelBox], field_box: PixelBox) -> list[tuple[int, int, int, int]]:
    """Field-local (y0,y1,x0,x1) slices for each slot, for feature extraction."""
    bounds = []
    for slot in slot_boxes:
        bounds.append((
            slot.y0 - field_box.y0,
            slot.y1 - field_box.y0,
            slot.x0 - field_box.x0,
            slot.x1 - field_box.x0,
        ))
    return bounds
