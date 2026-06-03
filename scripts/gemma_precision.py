"""Precision check: Gemma-4-31b + the DIRTY-rubric prompt over all local candidate crops.

A clean, well-calibrated model should call almost all of them CLEAN (base rate ~1%),
while still catching the known 131 (row4 of the 015 acta). Prints the DIRTY list.
"""
import base64
import glob
import io
import os
import re
from pathlib import Path

import requests
from PIL import Image

for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

KEY = os.environ["E14_OPENROUTER_API_KEY"]
BASE = "https://openrouter.ai/api/v1"
MODEL = "google/gemma-4-31b-it"
PROMPT = (
    "Analyze this image from a poll result. It consists of 3 digits tops, and some "
    "times when the digit is not filled out they put a placeholder like a dash, "
    "asterisk, dot or others. We're looking for anomalies like a number written on "
    "top of a placeholder trying to override it. Look for any strange sign in the "
    "image or not. Respond ONLY CLEAN or DIRTY"
)


def data_uri(path, max_px=256):
    raw = Path(path).read_bytes()
    with Image.open(io.BytesIO(raw)) as img:
        if max(img.size) > max_px:
            img = img.convert("RGB")
            img.thumbnail((max_px, max_px), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            raw = buf.getvalue()
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def verdict(path):
    content = [{"type": "text", "text": PROMPT},
               {"type": "image_url", "image_url": {"url": data_uri(path)}}]
    r = requests.post(f"{BASE}/chat/completions",
                      headers={"Authorization": f"Bearer {KEY}"},
                      json={"model": MODEL, "temperature": 0.0, "max_tokens": 20,
                            "messages": [{"role": "user", "content": content}]},
                      timeout=90)
    r.raise_for_status()
    txt = (r.json()["choices"][0]["message"]["content"] or "").upper()
    m = re.search(r"\b(DIRTY|CLEAN)\b", txt)
    return m.group(1) if m else "?"


crops = sorted(p for p in glob.glob("data/_cvprobe/crops/*candidate_field.png") if "enhanced" not in p)
dirty, clean, unk = [], 0, []
for p in crops:
    v = verdict(p)
    name = Path(p).name.replace("E14_PRE_01_001_013_04_015_delegados_", "015_").replace("_candidate_field.png", "")
    if v == "DIRTY":
        dirty.append(name)
    elif v == "CLEAN":
        clean += 1
    else:
        unk.append(name)
    print(f"  {v:6s} {name}")

print(f"\nTotal {len(crops)} | CLEAN {clean} | DIRTY {len(dirty)} | ? {len(unk)}")
print(f"DIRTY-flagged: {dirty}")
print(f"131 row4 in dirty list: {'015_p1_row4' in dirty}")
