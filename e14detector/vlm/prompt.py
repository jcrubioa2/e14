"""Prompts for VLM inspection of vote-count crops.

Design rule: the DEFINITION of DIRTY vs CLEAN (``_RUBRIC``) is **identical** across
every path, so "strange" means the same thing whether a crop was caught by the
proactive pre-screen, confirmed after an upvote, or re-read after a downvote. If the
bar differed between paths the system would be incoherent and gameable (publish under
a loose bar, never clear under a strict one). Only a one-line *framing* differs per
path, and each adjudication framing leans AGAINST the action it triggers — adding
hysteresis so neither a "suspicious" mob nor a "looks normal" mob can move a verdict
by sheer volume; the model must independently see (or not see) the overlap.
"""
from __future__ import annotations

# The shared rubric — same wording everywhere. Only the lead-in sentence changes.
_RUBRIC = """It is a poll count with at most 3 digits. The poll judge fills any unused digit \
position with a plain placeholder mark — an asterisk (*), a dash (-) or a dot (.). A row made \
only of these marks just means a low or zero count and is completely normal.

Answer with ONLY one word — CLEAN or DIRTY — and nothing else.
CLEAN = ordinary separate digits and/or plain placeholder marks, with no overlap.
DIRTY = a real digit is written ON TOP OF a placeholder mark (clearly overlapping or merged \
with it), or other clear tampering."""

# 1) Proactive 5% pre-screen — no human prior, so read it plainly (no lean). This SEEDS
#    the public basis, where false positives are most costly; it must not hunt for fraud.
VOTE_FIELD_SCREEN_PROMPT = (
    "Read the handwritten number on the RIGHT side of this crop and decide whether it is "
    "ordinary or shows tampering. Do not assume foul play.\n\n" + _RUBRIC
)

# 2) Upvote confirmation — the crowd already pushed toward "suspicious" and we are about to
#    PUBLISH. Lean skeptical of the report so a mob cannot get a clean crop published.
VOTE_FIELD_CONFIRM_PROMPT = (
    "Some reviewers flagged the handwritten number on the RIGHT side of this crop as possibly "
    "altered, but such reports are often mistaken. Judge it independently and only call it "
    "DIRTY if you yourself can clearly see a digit written over a placeholder.\n\n" + _RUBRIC
)

# 3) Downvote appeal — the crowd pushed toward "normal" and we are about to UN-publish. Lean
#    careful so a mob cannot launder a genuine overlap; but a plain row of placeholder dots
#    still reads CLEAN (there is no digit), which is exactly how a real false positive clears.
VOTE_FIELD_APPEAL_PROMPT = (
    "The handwritten number on the RIGHT side of this crop was marked as possibly altered; some "
    "viewers believe it is actually normal. Before clearing it, look carefully and only call it "
    "CLEAN if you are confident no real digit is written over a placeholder.\n\n" + _RUBRIC
)

# Default used by the adapter when a caller passes no explicit prompt: the neutral screen
# read (the safe, no-lean default). Live paths pass CONFIRM/APPEAL explicitly.
VOTE_FIELD_REVIEW_PROMPT = VOTE_FIELD_SCREEN_PROMPT
