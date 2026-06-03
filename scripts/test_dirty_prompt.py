"""Sweep VLM models on the decisive trio: 131 (DIRTY) vs two clean dot rows (CLEAN).

Goal: find a model that flips 131 to DIRTY WITHOUT false-positiving the clean
placeholder-dot rows. Temp 0; one shot per (model x crop x res).
"""
import base64
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

PROMPT = (
    "Analyze this image from a poll result. It consists of 3 digits tops, and some "
    "times when the digit is not filled out they put a placeholder like a dash, "
    "asterisk, dot or others. We're looking for anomalies like a number written on "
    "top of a placeholder trying to override it. Look for any strange sign in the "
    "image or not. Respond ONLY CLEAN or DIRTY"
)

MODELS = [
    "google/gemma-3-27b-it",
    "google/gemma-4-31b-it",
    "qwen/qwen3-vl-8b-instruct",
    "qwen/qwen3-vl-8b-thinking",
    "qwen/qwen3-vl-32b-instruct",
    "qwen/qwen3-vl-30b-a3b-thinking",
    "qwen/qwen2.5-vl-72b-instruct",
    "qwen/qwen3.6-flash",
    "google/gemini-2.5-flash-lite",
    "google/gemini-2.5-flash",
    "google/gemini-3.1-flash-lite",
]

CROPS = {
    "131->DIRTY": "data/_cvprobe/crops/E14_PRE_01_001_013_04_015_delegados_p1_row4_candidate_field.png",
    "dots3->CLEAN": "data/_cvprobe/crops/E14_PRE_01_001_013_04_015_delegados_p1_row3_candidate_field.png",
    "dots5->CLEAN": "data/_cvprobe/crops/E14_PRE_01_001_013_04_015_delegados_p1_row5_candidate_field.png",
}
RES = [256, 0]  # 0 = full


def data_uri(path, max_px):
    raw = Path(path).read_bytes()
    if max_px:
        with Image.open(io.BytesIO(raw)) as img:
            if max(img.size) > max_px:
                img = img.convert("RGB")
                img.thumbnail((max_px, max_px), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                raw = buf.getvalue()
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def verdict(model, path, max_px):
    content = [{"type": "text", "text": PROMPT},
               {"type": "image_url", "image_url": {"url": data_uri(path, max_px)}}]
    try:
        r = requests.post(f"{BASE}/chat/completions",
                          headers={"Authorization": f"Bearer {KEY}"},
                          json={"model": model, "temperature": 0.0, "max_tokens": 60,
                                "messages": [{"role": "user", "content": content}]},
                          timeout=90)
        r.raise_for_status()
        txt = (r.json()["choices"][0]["message"]["content"] or "").upper()
        m = re.search(r"\b(DIRTY|CLEAN)\b", txt)
        return m.group(1)[0] if m else "?"  # D / C / ?
    except Exception as e:
        return f"ERR({str(e)[:20]})"


print(f"{'model':32s} | res |  131  dots3 dots5  | verdict")
print("-" * 72)
for model in MODELS:
    for res in RES:
        v = {name: verdict(model, path, res) for name, path in CROPS.items()}
        ok = v["131->DIRTY"] == "D" and v["dots3->CLEAN"] == "C" and v["dots5->CLEAN"] == "C"
        tag = "<<< WINNER" if ok else ("catches131" if v["131->DIRTY"] == "D" else "")
        label = f"{res}px" if res else "full"
        print(f"{model:32s} | {label:4s}|  {v['131->DIRTY']:4s} {v['dots3->CLEAN']:4s} {v['dots5->CLEAN']:4s} | {tag}")
