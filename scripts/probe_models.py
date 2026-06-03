"""Cheap K=3 consensus probe of candidate VLMs on the small labeled set.

Runs each model 3x (temp>0) over files/good (expect CLEAN) and files/bad (expect
DIRTY) via OpenRouter, prints the per-crop vote string (e.g. "DDD"/"DCC") and a
recall/specificity summary using a MAJORITY vote. The vote string itself is the
signal: a stable anomaly votes DDD; the undetectable fused-dot class wobbles.

Not a big sweep — just enough to see if kimi-k2.5 / step-3.7-flash surprise us.
"""
import base64
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

KEY = os.environ["E14_OPENROUTER_API_KEY"]
BASE = "https://openrouter.ai/api/v1"

# The prompt the user highlighted (bare CLEAN/DIRTY rubric, our baseline).
PROMPT = (
    "Analyze this image from a poll result. It consists of 3 digits tops, and some "
    "times when the digit is not filled out they put a placeholder like a dash, "
    "asterisk, dot or others. We're looking for anomalies like a number written on "
    "top of a placeholder trying to override it. Look for any strange sign in the "
    "image or not. Respond ONLY CLEAN or DIRTY"
)

MODELS = [
    "google/gemma-4-31b-it",      # precision anchor / control
    "moonshotai/kimi-k2.5",       # candidate
    "stepfun/step-3.7-flash",     # candidate
]

K = 3
MAX_PX = 384
TEMP = 0.5

# Ground-truth notes for the 5 bad crops (Class A = visibly tamperable, B = fused/undetectable).
BAD_NOTE = {
    "image.png": "A:struck*+slash8",
    "image copy 2.png": "A:slashed-0",
    "image copy.png": "X:139 cross-crop",
    "image copy 3.png": "B:52 fused-dot",
    "image copy 4.png": "B:131 fused",
}


def data_uri(path, max_px=MAX_PX):
    raw = Path(path).read_bytes()
    with Image.open(io.BytesIO(raw)) as img:
        if max(img.size) > max_px:
            img = img.convert("RGB")
            img.thumbnail((max_px, max_px), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            raw = buf.getvalue()
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def one_call(model, uri):
    content = [{"type": "text", "text": PROMPT},
               {"type": "image_url", "image_url": {"url": uri}}]
    try:
        r = requests.post(f"{BASE}/chat/completions",
                          headers={"Authorization": f"Bearer {KEY}"},
                          json={"model": model, "temperature": TEMP, "max_tokens": 20,
                                "messages": [{"role": "user", "content": content}]},
                          timeout=120)
        r.raise_for_status()
        txt = (r.json()["choices"][0]["message"]["content"] or "").upper()
        m = re.search(r"\b(DIRTY|CLEAN)\b", txt)
        return m.group(1)[0] if m else "?"
    except Exception as e:  # noqa: BLE001
        return f"E"


def votes(model, uri):
    return "".join(one_call(model, uri) for _ in range(K))


def main():
    jobs = []  # (label, path, uri)
    for label in ("good", "bad"):
        for p in sorted((ROOT / "files" / label).glob("*.png")):
            jobs.append((label, p, data_uri(p)))
    print(f"{len(jobs)} crops x {len(MODELS)} models x K={K} @ {MAX_PX}px temp={TEMP}\n")

    for model in MODELS:
        with ThreadPoolExecutor(max_workers=12) as ex:
            res = list(ex.map(lambda j: (j[0], j[1], votes(model, j[2])), jobs))
        tp = fp = tn = fn = 0
        print(f"=== {model} ===")
        for label, p, v in res:
            dirty = v.count("D") >= (K // 2 + 1)  # majority
            if label == "bad":
                note = BAD_NOTE.get(p.name, "")
                ok = "OK " if dirty else "MISS"
                print(f"  [{ok}] bad  {v:4} {note}")
                tp += dirty
                fn += not dirty
            else:
                if dirty:
                    print(f"  [FP ] good {v:4} {p.name}")
                fp += dirty
                tn += not dirty
        n_bad, n_good = tp + fn, tn + fp
        print(f"  recall {tp}/{n_bad}  specificity {tn}/{n_good} "
              f"(FP={fp})\n")


if __name__ == "__main__":
    main()
