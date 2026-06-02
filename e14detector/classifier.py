"""Conservative deterministic classification heuristics."""
from __future__ import annotations

from dataclasses import dataclass

from .cv_features import SlotFeatures
from .schemas import FieldClassification, SlotClass


@dataclass(frozen=True)
class SlotClassification:
    slot_class: SlotClass
    placeholder_overlap_score: float
    reason: str


@dataclass(frozen=True)
class FieldClassificationResult:
    final_classification: FieldClassification
    placeholder_overlap_score: float
    digit_shape_score: float
    cv_score: float
    needs_human_review: bool
    reason: str
    anomaly_tags: tuple[str, ...]


def classify_slot(features: SlotFeatures) -> SlotClassification:
    density = features.ink_density
    if density < 0.003 or features.component_count == 0:
        return SlotClassification(SlotClass.BLANK, 0.0, "blank or near-blank slot")
    if features.mixed_component_score >= 0.55:
        return SlotClassification(SlotClass.MIXED, features.mixed_component_score, "digit-like and placeholder-like marks coexist")
    if features.placeholder_like_component_count and not features.digit_like_component_count:
        return SlotClassification(SlotClass.PLACEHOLDER, 0.05, "small compact placeholder-like mark")
    if features.digit_like_component_count and density >= 0.015:
        score = 0.25 if features.component_count > 2 else 0.05
        return SlotClassification(SlotClass.DIGIT, score, "digit-like stroke")
    return SlotClassification(SlotClass.UNCLEAR, 0.35, "ambiguous mark")


def classify_field(
    slot_features: list[SlotFeatures],
    digit_shape_score: float = 0.0,
    crop_failed: bool = False,
    shape_anomaly_slot: int | None = None,
) -> FieldClassificationResult:
    if crop_failed:
        return FieldClassificationResult(
            final_classification=FieldClassification.CROP_FAILED,
            placeholder_overlap_score=0.0,
            digit_shape_score=0.0,
            cv_score=0.0,
            needs_human_review=True,
            reason="crop failed; needs human review",
            anomaly_tags=("crop_failed",),
        )

    slots = [classify_slot(features) for features in slot_features]
    overlap_score = max((slot.placeholder_overlap_score for slot in slots), default=0.0)
    tags: list[str] = []

    if overlap_score >= 0.55:
        tags.append("placeholder_overlap")
        final = FieldClassification.SUSPICIOUS_OVERLAP
        reason = "possible visual anomaly: digit-like and placeholder-like marks overlap in a slot"
    elif digit_shape_score >= 0.65:
        tags.append("digit_shape_inconsistency")
        if shape_anomaly_slot == 1:
            tags.append("possible_leading_digit_alteration")
        final = FieldClassification.DIGIT_SHAPE_ANOMALY
        reason = "possible visual anomaly: digit shape differs meaningfully from local context"
    elif (
        len(slot_features) >= 3
        and all(slot.slot_class == SlotClass.DIGIT for slot in slots[:3])
        and all(features.spiky_component_score >= 0.85 for features in slot_features[:3])
    ):
        final = FieldClassification.CLEAN
        reason = "all slots appear to contain filler marks"
    elif (
        len(slot_features) >= 3
        and all(slot.slot_class == SlotClass.DIGIT for slot in slots[:3])
        and slot_features[0].spiky_component_score >= 0.85
        and 0.45 <= slot_features[1].spiky_component_score < 0.85
        and slot_features[2].spiky_component_score < 0.25
    ):
        tags.append("leading_placeholder_digit_ambiguity")
        final = FieldClassification.UNCLEAR
        reason = "leading mark could be a placeholder or altered digit; needs human review"
    elif overlap_score >= 0.30 or digit_shape_score >= 0.30 or any(s.slot_class == SlotClass.UNCLEAR for s in slots):
        final = FieldClassification.UNCLEAR
        reason = "unclear mark; needs human review"
    else:
        final = FieldClassification.CLEAN
        reason = "no deterministic visual anomaly detected"

    return FieldClassificationResult(
        final_classification=final,
        placeholder_overlap_score=round(overlap_score, 4),
        digit_shape_score=round(digit_shape_score, 4),
        cv_score=round(max(overlap_score, digit_shape_score), 4),
        needs_human_review=final in {
            FieldClassification.SUSPICIOUS_OVERLAP,
            FieldClassification.DIGIT_SHAPE_ANOMALY,
            FieldClassification.UNCLEAR,
        },
        reason=reason,
        anomaly_tags=tuple(tags),
    )
