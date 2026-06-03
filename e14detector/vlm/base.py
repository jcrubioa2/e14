"""Base interfaces for optional VLM review."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from e14detector.schemas import FieldClassification

_VERDICT_RE = re.compile(r"\b(CLEAN|DIRTY)\b", re.IGNORECASE)


@dataclass(frozen=True)
class VLMReviewResult:
    classification: FieldClassification
    confidence: float
    read_value: str | None
    raw_json: dict
    reason: str


class VisionReviewer(Protocol):
    def review_vote_field(
        self,
        image_paths: list[str],
        metadata: dict,
        thinking_budget: int | None = None,
        prompt_text: str | None = None,
        temperature: float | None = None,
    ) -> VLMReviewResult:
        ...


def _coerce_json(text: str) -> dict | None:
    """Parse a JSON object directly or embedded in surrounding text, else None."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def _from_payload(payload: dict) -> VLMReviewResult:
    if "verdict" in payload and "classification" not in payload:
        verdict = str(payload["verdict"]).strip().upper()
        classification = (
            FieldClassification.SUSPICIOUS_OVERLAP
            if verdict.startswith("DIRTY")
            else FieldClassification.CLEAN
        )
    else:
        classification = FieldClassification(str(payload["classification"]))
    confidence = float(payload.get("confidence", 1.0))
    return VLMReviewResult(
        classification=classification,
        confidence=max(0.0, min(1.0, confidence)),
        read_value=payload.get("read_value"),
        raw_json=payload,
        reason=str(payload.get("reason", "")),
    )


def parse_vlm_json(text: str) -> VLMReviewResult:
    """Parse a VLM reply. The models now answer with a bare word (CLEAN/DIRTY), but we
    still accept JSON so cached verdicts and any JSON-mode model keep working.

    The confidence score is intentionally not requested anymore (a labeled check proved
    it carries no usable signal); a bare-word verdict is recorded with confidence 1.0.
    """
    payload = _coerce_json(text)
    if payload is not None and ("verdict" in payload or "classification" in payload):
        try:
            return _from_payload(payload)
        except Exception:
            pass  # malformed JSON verdict: fall through to the bare-word scan
    # Bare word: take the LAST CLEAN/DIRTY token so a thinking model's reasoning preamble
    # (which may mention both words) doesn't override its final answer.
    matches = _VERDICT_RE.findall(text or "")
    if matches:
        verdict = matches[-1].upper()
        classification = (
            FieldClassification.SUSPICIOUS_OVERLAP
            if verdict == "DIRTY"
            else FieldClassification.CLEAN
        )
        return VLMReviewResult(classification, 1.0, None, {"verdict": verdict}, "")
    raise ValueError("VLM_INVALID_JSON")
