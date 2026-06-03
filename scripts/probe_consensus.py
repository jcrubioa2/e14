"""End-to-end smoke test of the poll consensus path: gemma-4-31b, K reads, majority.

Mirrors webapp._review_crop_consensus using the real provider (so it exercises the
temperature plumbing and the CONFIRM prompt), then applies the same majority rule:
PUBLISH-as-strange iff 2*n_strange > k. Prints the per-crop tally + decision over
files/good (expect CLEAN/keep) and files/bad (Class A should publish; Class B should
NOT reach a majority -> stays re-eligible, which is the honest outcome).
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from e14detector import config  # noqa: E402
from e14detector.vlm.factory import build_reviewer  # noqa: E402
from e14detector.vlm.prompt import VOTE_FIELD_CONFIRM_PROMPT  # noqa: E402
from e14detector.webapp import STRANGE_CLASSES  # noqa: E402

K = config.POLL_CONSENSUS_K
TEMP = config.POLL_CONSENSUS_TEMP
reviewer = build_reviewer("openrouter", model="google/gemma-4-31b-it")

BAD_NOTE = {
    "image.png": "A:struck*+slash8",
    "image copy 2.png": "A:slashed-0",
    "image copy.png": "X:139 cross-crop",
    "image copy 3.png": "B:52 fused-dot",
    "image copy 4.png": "B:131 fused",
}


def tally(path):
    def one(_):
        r = reviewer.review_vote_field([str(path)], metadata={},
                                       prompt_text=VOTE_FIELD_CONFIRM_PROMPT, temperature=TEMP)
        return r.classification in STRANGE_CLASSES
    with ThreadPoolExecutor(max_workers=K) as ex:
        votes = list(ex.map(one, range(K)))
    return sum(votes), len(votes)


print(f"gemma-4-31b consensus K={K} temp={TEMP} @ {config.QWEN_MAX_IMAGE_PX}px (CONFIRM prompt)\n")
for label in ("bad", "good"):
    print(f"=== {label} ===")
    for p in sorted((ROOT / "files" / label).glob("*.png")):
        n, k = tally(p)
        publish = 2 * n > k
        note = BAD_NOTE.get(p.name, p.name) if label == "bad" else p.name
        print(f"  {n}/{k} {'PUBLISH ' if publish else 're-elig.':8} {note}")
    print()
