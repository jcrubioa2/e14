"""Prompt text for optional VLM inspection."""
from __future__ import annotations

VOTE_FIELD_REVIEW_PROMPT = """You are inspecting a cropped handwritten vote-count field from an election form.

The field has exactly three slots because the value can be up to three digits.
Unused slots may contain placeholder marks such as dots, dashes, asterisks, or small filler marks.
Placeholder marks in unused leading slots are normal and should not be treated
as suspicious by themselves. A field made only of placeholder marks is also not
a visual anomaly by itself. Read only the actual digit marks as the vote value.

Inspect only for possible visual anomalies:
1. Placeholder overlap: a digit appears written on top of, overlapping, replacing, or visually merging with a placeholder mark.
2. Digit-shape anomaly: a digit appears visually inconsistent, slash-like, overwritten, retraced, unusually angled, unusually sized, or meaningfully different from provided comparison digits.

Use UNCLEAR if the difference could reasonably be normal handwriting variation.
Do not claim fraud, tampering, forgery, or intent.

The "classification" value MUST be exactly one of these strings (no other words):
- "CLEAN": no visual anomaly; ordinary digits and/or placeholder marks.
- "SUSPICIOUS_OVERLAP": a digit overlaps/replaces/merges with a placeholder mark.
- "DIGIT_SHAPE_ANOMALY": a digit's shape looks inconsistent, retraced, or overwritten.
- "UNCLEAR": possibly anomalous but could be normal handwriting variation.
Do not invent any other label (for example, never return "NORMAL").

Return strict JSON only with:
classification, confidence, read_value, slot_analysis, comparison_used, comparison_notes, reason.
"""
