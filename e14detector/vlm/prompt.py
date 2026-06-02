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


# Neutral re-evaluation prompt used by the community APPEAL path ("Se ve normal").
# It deliberately does NOT prime the model toward fraud ("a common dirty game is…"),
# because that priming is what manufactures false positives on plain placeholder
# dots. It is balanced, NOT lenient: a real digit written on top of a placeholder
# still reads DIRTY, so vote-stuffing cannot launder a genuine overlap. The judge of
# an appeal is still the model — the crowd only triggers the re-read.
VOTE_FIELD_APPEAL_PROMPT = """Read the handwritten poll count on the RIGHT side of this crop. \
It has at most 3 digits. Any unused digit position is filled by the poll judge with a plain \
placeholder mark — an asterisk (*), a dash (-) or a dot (.). These placeholder marks are normal \
and expected; a row of them alone just means a low or zero count and is perfectly ordinary.

Do not assume foul play. Decide plainly: are these ordinary separate digits and/or plain \
placeholder marks, or is a real digit genuinely written ON TOP OF a placeholder (clearly \
overlapping or merged with it)?

Answer with ONLY a compact JSON object and nothing else:
{"verdict": "DIRTY" or "CLEAN", "confidence": a number from 0 to 1}
"DIRTY" = a digit clearly overlaps/covers a placeholder mark. \
"CLEAN" = ordinary digits and/or plain placeholder marks, no overlap."""
