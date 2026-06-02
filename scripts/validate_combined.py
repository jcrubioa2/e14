"""Conjoined CV + VLM evaluation on the labeled crops.

Runs BOTH passes on each crop, then scores several gate policies so we can see
what combining actually buys vs each alone:

  CV_ONLY      : CV needs_human_review
  VLM_ONLY     : VLM flags an anomaly (dot-aware prompt)
  VLM_VETO     : current design — CV flags, but a VLM "clean" hides the row
  NAIVE_UNION  : flag if CV OR VLM fires
  NONVETO_UNION: CV-strong (real overlap OR shape>=0.65) is unvetoable; the
                 marginal CV band is only kept if the VLM agrees; VLM anomalies
                 on CV-clean rows are added.

  files/good/* -> expect NOT flagged   files/bad/* -> expect flagged
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14detector import config  # noqa: E402
from e14detector.classifier import classify_field, classify_slot  # noqa: E402
from e14detector.cv_features import extract_slot_features  # noqa: E402
from e14detector.digit_shape import digit_shape_score, extract_digit_shape_features  # noqa: E402
from e14detector.layout import PixelBox  # noqa: E402
from e14detector.preprocess import preprocess_for_features  # noqa: E402
from e14detector.schemas import SlotClass  # noqa: E402
from e14detector.segmentation import adaptive_slot_boxes  # noqa: E402
from e14detector.vlm.alibaba_qwen_provider import _data_uri  # noqa: E402

IGNORE = {"image copy.png"}  # 139: cross-crop, out of scope

DOT_AWARE = """You are inspecting a cropped handwritten vote-count field (up to three digits) from a Colombian E-14 form.

Unused slots may hold ONE small standalone filler mark (dot, dash, asterisk) in an otherwise empty slot - that is NORMAL. But a digit written ON TOP OF / through / merged with a mark (a dot or dash visible BEHIND, UNDER, or touching a digit's strokes), or a digit that is struck through, slashed, retraced, or doubled, is an ANOMALY worth human review.

classification MUST be exactly one of: "CLEAN", "SUSPICIOUS_OVERLAP", "DIGIT_SHAPE_ANOMALY", "UNCLEAR". Never any other label.
Return strict JSON: classification, confidence, read_value, reason.
"""
ANOM = {"SUSPICIOUS_OVERLAP", "DIGIT_SHAPE_ANOMALY", "UNCLEAR"}


def cv_pass(path: Path):
    img = np.array(Image.open(path).convert("RGB"))
    h, w = img.shape[:2]
    fb = PixelBox(0, 0, w, h)
    fbin = preprocess_for_features(img)
    boxes = adaptive_slot_boxes(fb, fbin)
    feats, bins = [], []
    for b in boxes:
        b = b.normalized()
        bb = preprocess_for_features(img[b.y0:b.y1, b.x0:b.x1])
        feats.append(extract_slot_features(bb))
        bins.append(bb)
    sres = [classify_slot(f) for f in feats]
    shapes = [digit_shape_score(extract_digit_shape_features(bb)) if r.slot_class == SlotClass.DIGIT else 0.0
              for bb, r in zip(bins, sres)]
    mx = max(shapes, default=0.0)
    slot = (shapes.index(mx) + 1) if mx >= 0.65 else None
    final = classify_field(feats, digit_shape_score=mx, shape_anomaly_slot=slot)
    overlap = max((s.placeholder_overlap_score for s in sres), default=0.0)
    return {
        "flag": final.needs_human_review,
        "overlap": round(overlap, 3),
        "shape": round(final.digit_shape_score, 3),
        "cls": final.final_classification.value,
    }


def vlm_pass(path: Path):
    uri = _data_uri(str(path), config.QWEN_MAX_IMAGE_PX)
    payload = {"model": "qwen3.6-flash", "messages": [{"role": "user", "content": [
        {"type": "text", "text": DOT_AWARE}, {"type": "image_url", "image_url": {"url": uri}}]}],
        "temperature": 0.0, "response_format": {"type": "json_object"}}
    r = requests.post(f"{config.QWEN_BASE_URL}/chat/completions",
                      headers={"Authorization": f"Bearer {config.QWEN_API_KEY}", "Content-Type": "application/json"},
                      json=payload, timeout=90)
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    s, e = txt.find("{"), txt.rfind("}")
    pl = json.loads(txt[s:e + 1])
    return {"cls": str(pl.get("classification")), "anom": str(pl.get("classification")) in ANOM}


def policies(cv, vlm):
    cv_strong = cv["overlap"] >= 0.55 or cv["shape"] >= 0.65
    return {
        "CV_ONLY": cv["flag"],
        "VLM_ONLY": vlm["anom"],
        "VLM_VETO": cv["flag"] and vlm["anom"],
        "NAIVE_UNION": cv["flag"] or vlm["anom"],
        "NONVETO_UNION": cv_strong or vlm["anom"] or (cv["flag"] and vlm["anom"]),
    }


def main() -> None:
    jobs = [("good", p) for p in sorted((ROOT / "files/good").glob("*.png"))]
    jobs += [("bad", p) for p in sorted((ROOT / "files/bad").glob("*.png")) if p.name not in IGNORE]

    def work(j):
        lab, p = j
        return lab, p, cv_pass(p), vlm_pass(p)

    rows = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for lab, p, cv, vlm in ex.map(work, jobs):
            rows.append((lab, p, cv, vlm, policies(cv, vlm)))

    print(f"{'label':5} {'file':18} {'CV':22} {'VLM':18} ov/shape")
    for lab, p, cv, vlm, _ in rows:
        print(f"{lab:5} {p.name:18} {cv['cls']:22} {vlm['cls']:18} {cv['overlap']}/{cv['shape']}")

    names = list(rows[0][4].keys())
    n_bad = sum(1 for lab, *_ in rows if lab == "bad")
    n_good = sum(1 for lab, *_ in rows if lab == "good")
    print(f"\n{'policy':14} recall(bad)   specificity(good)   missed bad")
    for name in names:
        tp = sum(1 for lab, p, cv, vlm, pol in rows if lab == "bad" and pol[name])
        tn = sum(1 for lab, p, cv, vlm, pol in rows if lab == "good" and not pol[name])
        missed = [p.name for lab, p, cv, vlm, pol in rows if lab == "bad" and not pol[name]]
        print(f"{name:14} {tp}/{n_bad} = {tp/n_bad:4.0%}    {tn}/{n_good} = {tn/n_good:4.0%}        {missed}")


if __name__ == "__main__":
    main()
