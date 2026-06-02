"""Base interfaces for optional VLM review."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from e14detector.schemas import FieldClassification


@dataclass(frozen=True)
class VLMReviewResult:
    classification: FieldClassification
    confidence: float
    read_value: str | None
    raw_json: dict
    reason: str


class VisionReviewer(Protocol):
    def review_vote_field(
        self, image_paths: list[str], metadata: dict, thinking_budget: int | None = None
    ) -> VLMReviewResult:
        ...


def parse_vlm_json(text: str) -> VLMReviewResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("VLM_INVALID_JSON") from exc
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as nested_exc:
            raise ValueError("VLM_INVALID_JSON") from nested_exc
    try:
        classification = FieldClassification(str(payload["classification"]))
    except Exception as exc:
        raise ValueError("VLM_INVALID_JSON") from exc
    confidence = float(payload.get("confidence", 0.0))
    return VLMReviewResult(
        classification=classification,
        confidence=max(0.0, min(1.0, confidence)),
        read_value=payload.get("read_value"),
        raw_json=payload,
        reason=str(payload.get("reason", "")),
    )
