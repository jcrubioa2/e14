"""Digit morphology scoring for conservative anomaly review."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .preprocess import connected_components


@dataclass(frozen=True)
class DigitShapeFeatures:
    width: int
    height: int
    ink_density: float
    component_count: int
    bounding_box: tuple[int, int, int, int] | None
    bbox_width: int
    bbox_height: int
    height_width_ratio: float
    slant_angle: float
    relative_size: float
    slash_like_score: float
    retrace_score: float


def _principal_axis_angle(points: np.ndarray) -> float:
    if len(points) < 2:
        return 0.0
    centered = points - points.mean(axis=0)
    _vals, vecs = np.linalg.eigh(np.cov(centered, rowvar=False))
    vx, vy = vecs[:, -1]
    angle_from_horizontal = float(np.degrees(np.arctan2(vy, vx)))
    angle_from_vertical = 90.0 - abs(angle_from_horizontal)
    return round(angle_from_vertical, 3)


def extract_digit_shape_features(binary_slot: np.ndarray) -> DigitShapeFeatures:
    if binary_slot.ndim != 2:
        raise ValueError("digit shape extraction expects a single-channel binary image")
    height, width = binary_slot.shape
    area = max(1, width * height)
    components = connected_components(binary_slot)
    largest = max(components, key=lambda c: c.area, default=None)
    ink = int(np.count_nonzero(binary_slot))

    if largest is None:
        return DigitShapeFeatures(
            width=width,
            height=height,
            ink_density=0.0,
            component_count=0,
            bounding_box=None,
            bbox_width=0,
            bbox_height=0,
            height_width_ratio=0.0,
            slant_angle=0.0,
            relative_size=0.0,
            slash_like_score=0.0,
            retrace_score=0.0,
        )

    crop = binary_slot[largest.y:largest.y + largest.height, largest.x:largest.x + largest.width]
    ys, xs = np.nonzero(crop)
    angle = _principal_axis_angle(np.column_stack([xs, ys])) if len(xs) else 0.0
    ratio = largest.height / max(1, largest.width)
    density = ink / area
    local_density = largest.area / max(1, largest.width * largest.height)
    slash_like = 0.0
    if ratio >= 2.2 and abs(angle) >= 12:
        slash_like = min(1.0, 0.45 + abs(angle) / 60)
    retrace = min(1.0, max(0.0, local_density - 0.38) * 2.0)

    return DigitShapeFeatures(
        width=width,
        height=height,
        ink_density=round(density, 6),
        component_count=len(components),
        bounding_box=largest.bbox,
        bbox_width=largest.width,
        bbox_height=largest.height,
        height_width_ratio=round(ratio, 4),
        slant_angle=angle,
        relative_size=round(largest.area / area, 6),
        slash_like_score=round(slash_like, 4),
        retrace_score=round(retrace, 4),
    )


def digit_shape_score(features: DigitShapeFeatures, comparison_mismatch_score: float = 0.0) -> float:
    score = max(features.slash_like_score, features.retrace_score, comparison_mismatch_score)
    if features.component_count > 2:
        score = max(score, 0.35)
    return round(min(1.0, score), 4)
