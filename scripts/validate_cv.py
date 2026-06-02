"""CV-only check: run the deterministic field pipeline over the labeled crops.

Mirrors processor.py's per-field CV path (no VLM, no network) so we can see what
the cheap pre-filter catches on its own and whether the VLM adds anything.

  files/good/* -> expected CLEAN (needs_human_review = False)
  files/bad/*  -> expected FLAGGED (needs_human_review = True)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14detector.classifier import classify_field, classify_slot  # noqa: E402
from e14detector.cv_features import extract_slot_features  # noqa: E402
from e14detector.digit_shape import digit_shape_score, extract_digit_shape_features  # noqa: E402
from e14detector.layout import PixelBox  # noqa: E402
from e14detector.preprocess import preprocess_for_features  # noqa: E402
from e14detector.schemas import SlotClass  # noqa: E402
from e14detector.segmentation import adaptive_slot_boxes  # noqa: E402

IGNORE = {"image copy.png"}  # the 139 cross-crop case, out of scope for single crop


def cv_classify(path: Path):
    img = np.array(Image.open(path).convert("RGB"))
    h, w = img.shape[:2]
    field_box = PixelBox(0, 0, w, h)
    field_binary = preprocess_for_features(img)
    slot_boxes = adaptive_slot_boxes(field_box, field_binary)
    slot_features, slot_binaries = [], []
    for sb in slot_boxes:
        sb = sb.normalized()
        crop = img[sb.y0:sb.y1, sb.x0:sb.x1]
        b = preprocess_for_features(crop)
        slot_features.append(extract_slot_features(b))
        slot_binaries.append(b)
    slot_results = [classify_slot(f) for f in slot_features]
    shape_scores = [
        digit_shape_score(extract_digit_shape_features(b)) if r.slot_class == SlotClass.DIGIT else 0.0
        for b, r in zip(slot_binaries, slot_results)
    ]
    max_shape = max(shape_scores, default=0.0)
    shape_slot = (shape_scores.index(max_shape) + 1) if max_shape >= 0.65 else None
    final = classify_field(slot_features, digit_shape_score=max_shape, shape_anomaly_slot=shape_slot)
    overlaps = [round(s.placeholder_overlap_score, 2) for s in slot_results]
    return final, [r.slot_class.value for r in slot_results], overlaps


def main() -> None:
    jobs = [("good", p) for p in sorted((ROOT / "files/good").glob("*.png"))]
    jobs += [("bad", p) for p in sorted((ROOT / "files/bad").glob("*.png")) if p.name not in IGNORE]
    tp = fp = tn = fn = 0
    for lab, p in jobs:
        try:
            final, slots, overlaps = cv_classify(p)
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERR ] {lab:4} {p.name}: {exc}")
            continue
        flagged = final.needs_human_review
        ok = flagged == (lab == "bad")
        mark = "OK  " if ok else "MISS"
        print(f"  [{mark}] {lab:4} {p.name:18} -> {final.final_classification.value:20} "
              f"slots={slots} overlap={overlaps} shape={final.digit_shape_score}")
        if lab == "bad":
            tp += flagged; fn += not flagged
        else:
            fp += flagged; tn += not flagged
    print(f"\n  CV-only: recall {tp}/{tp+fn} (in-scope bad)  |  specificity {tn}/{tn+fp} good")


if __name__ == "__main__":
    main()
