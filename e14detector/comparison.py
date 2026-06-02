"""Intra-document comparison helpers for digit-shape review."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .digit_shape import DigitShapeFeatures


@dataclass(frozen=True)
class DigitComparisonResult:
    mismatch_score: float
    notes: str


def compare_digit_to_examples(
    target: DigitShapeFeatures,
    examples: list[DigitShapeFeatures],
) -> DigitComparisonResult:
    """Softly compare one digit against examples from the same document.

    This is not a handwriting identity claim. It only measures whether simple
    morphology is visually outlying enough to warrant review.
    """
    usable = [e for e in examples if e.bounding_box is not None]
    if target.bounding_box is None or len(usable) < 2:
        return DigitComparisonResult(0.0, "not enough comparable digit examples")

    ratios = [e.height_width_ratio for e in usable]
    densities = [e.ink_density for e in usable]
    slants = [abs(e.slant_angle) for e in usable]
    ratio_delta = abs(target.height_width_ratio - median(ratios)) / max(0.1, median(ratios))
    density_delta = abs(target.ink_density - median(densities)) / max(0.01, median(densities))
    slant_delta = abs(abs(target.slant_angle) - median(slants)) / 45.0
    score = min(1.0, max(ratio_delta, density_delta, slant_delta))
    if score >= 0.65:
        notes = "target digit morphology is an outlier against local examples"
    elif score >= 0.30:
        notes = "target digit morphology is somewhat different from local examples"
    else:
        notes = "target digit morphology is within local comparison range"
    return DigitComparisonResult(round(score, 4), notes)
