"""Harvest normalized digit-slot images for one-class anomaly modeling.

Clean digit slots are abundant and CV-identifiable for free, so we use them as the
"normal" training distribution. Each slot is binarized (the pipeline's own
preprocessing), cropped to its ink bounding box, aspect-preserving padded to a
square, and resized to 32x32 — MNIST-style normalization so digits are comparable.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14detector.classifier import classify_slot  # noqa: E402
from e14detector.cv_features import extract_slot_features  # noqa: E402
from e14detector.layout import PixelBox  # noqa: E402
from e14detector.preprocess import preprocess_for_features  # noqa: E402
from e14detector.schemas import SlotClass  # noqa: E402
from e14detector.segmentation import adaptive_slot_boxes  # noqa: E402

SZ = 32


def norm_slot(binary: np.ndarray) -> np.ndarray | None:
    ys, xs = np.nonzero(binary)
    if len(xs) < 8:
        return None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    crop = binary[y0:y1, x0:x1].astype(np.float32) / 255.0
    h, w = crop.shape
    s = max(h, w)
    pad = np.zeros((s, s), np.float32)
    pad[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = crop
    return cv2.resize(pad, (SZ, SZ), interpolation=cv2.INTER_AREA)


def slot_binaries(crop_path: str):
    img = np.array(Image.open(crop_path).convert("RGB"))
    h, w = img.shape[:2]
    fb = PixelBox(0, 0, w, h)
    fbin = preprocess_for_features(img)
    out = []
    for b in adaptive_slot_boxes(fb, fbin):
        b = b.normalized()
        bb = preprocess_for_features(img[b.y0:b.y1, b.x0:b.x1])
        out.append(bb)
    return out


def harvest_clean(db_path: str, cap: int | None = None) -> np.ndarray:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT raw_crop_path FROM vote_fields "
        "WHERE row_type='candidate' AND final_classification='CLEAN' AND raw_crop_path IS NOT NULL"
    ).fetchall()
    out = []
    for r in rows:
        p = r["raw_crop_path"]
        if not p or not Path(p).exists():
            continue
        try:
            bins = slot_binaries(p)
        except Exception:
            continue
        for bb in bins:
            if classify_slot(extract_slot_features(bb)).slot_class != SlotClass.DIGIT:
                continue
            n = norm_slot(bb)
            if n is not None:
                out.append(n)
        if cap and len(out) >= cap:
            break
    con.close()
    return np.array(out, np.float32)


def slot_from(db_path: str, doc: str, row: int, slot_idx: int) -> np.ndarray | None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    r = con.execute(
        "SELECT raw_crop_path FROM vote_fields WHERE document_id=? AND row_number=? AND row_type='candidate'",
        (doc, row),
    ).fetchone()
    con.close()
    if not r:
        return None
    bins = slot_binaries(r["raw_crop_path"])
    return norm_slot(bins[slot_idx]) if slot_idx < len(bins) else None


if __name__ == "__main__":
    dbs = [d for d in sys.argv[1:]] or ["data/detector/results/results.sqlite"]
    allc = [harvest_clean(d) for d in dbs]
    X = np.concatenate([a for a in allc if len(a)], axis=0)
    np.save(ROOT / "data" / "clean_digits.npy", X)
    print(f"harvested {len(X)} clean digit slots -> data/clean_digits.npy  shape={X.shape}")
