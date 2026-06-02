"""Deterministic VLM provider for tests and offline runs."""
from __future__ import annotations

from .base import VLMReviewResult
from e14detector.schemas import FieldClassification


class MockVisionReviewer:
    def __init__(self, classification: FieldClassification = FieldClassification.UNCLEAR):
        self.classification = classification

    def review_vote_field(
        self,
        image_paths: list[str],
        metadata: dict,
        thinking_budget: int | None = None,
        prompt_text: str | None = None,
    ) -> VLMReviewResult:
        return VLMReviewResult(
            classification=self.classification,
            confidence=0.5,
            read_value=None,
            raw_json={
                "classification": self.classification.value,
                "confidence": 0.5,
                "read_value": None,
                "reason": "mock review; needs human review",
                "image_count": len(image_paths),
                "metadata": metadata,
            },
            reason="mock review; needs human review",
        )
