"""Prompt text for optional VLM inspection."""
from __future__ import annotations

VOTE_FIELD_REVIEW_PROMPT = """We are detecting anomalies in the handwritten number on the RIGHT side of this crop. \
It is a poll count with at most 3 digits. The poll judge fills any missing digit position with \
a placeholder character — an asterisk (*), a dash (-) or a dot (.) — and the real digits should \
be clear, separate numbers.

A common dirty game is writing a NUMBER ON TOP OF a placeholder to inflate the count. Check \
carefully for any sign of a digit overlapping, covering or merging with a placeholder mark.

Answer with ONLY a compact JSON object and nothing else:
{"verdict": "DIRTY" or "CLEAN", "confidence": a number from 0 to 1}
"DIRTY" = you see a digit overlapping a placeholder, or any other tampering. \
"CLEAN" = ordinary digits and/or plain placeholder marks, no overlap."""
