"""Ad-hoc recall check: run the real Qwen reviewer over labeled crops.

Folders:
  files/good/*  -> expected CLEAN (must NOT be flagged)
  files/bad/*   -> expected FLAGGED (SUSPICIOUS_OVERLAP / DIGIT_SHAPE_ANOMALY / UNCLEAR)

Prints per-image verdicts and a precision/recall summary so we can decide
whether qwen3.6-flash is a strong enough gate before scaling.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e14detector import config  # noqa: E402
from e14detector.schemas import FieldClassification  # noqa: E402
from e14detector.vlm.factory import build_reviewer  # noqa: E402

FLAGGED = {
    FieldClassification.SUSPICIOUS_OVERLAP,
    FieldClassification.DIGIT_SHAPE_ANOMALY,
    FieldClassification.UNCLEAR,
}


def review(reviewer, path: Path):
    try:
        r = reviewer.review_vote_field([str(path)], {"candidate_name": path.stem})
        return path, r, None
    except Exception as exc:  # noqa: BLE001
        return path, None, exc


def main() -> None:
    reviewer = build_reviewer("qwen")
    jobs = []
    for label in ("good", "bad"):
        for p in sorted((ROOT / "files" / label).glob("*.png")):
            jobs.append((label, p))

    print(f"Reviewing {len(jobs)} crops with {config.QWEN_MODEL} "
          f"(thinking_budget={config.QWEN_THINKING_BUDGET}, max_px={config.QWEN_MAX_IMAGE_PX})\n")

    results = {}
    with ThreadPoolExecutor(max_workers=config.VLM_CONCURRENCY) as ex:
        futs = {ex.submit(review, reviewer, p): (label, p) for label, p in jobs}
        for fut in futs:
            label, p = futs[fut]
            results[(label, p)] = fut.result()

    # confusion counts
    tp = fp = tn = fn = err = 0
    for label, p in jobs:
        _, r, exc = results[(label, p)]
        if exc is not None:
            print(f"  [ERR ] {label:4} {p.name}: {exc}")
            err += 1
            continue
        flagged = r.classification in FLAGGED
        mark = "OK " if (flagged == (label == "bad")) else "MISS"
        print(f"  [{mark}] {label:4} {p.name:22} -> {r.classification.value:20} "
              f"conf={r.confidence:.2f} read={r.read_value!r}")
        if label == "bad":
            if flagged:
                tp += 1
            else:
                fn += 1
        else:
            if flagged:
                fp += 1
            else:
                tn += 1

    n_bad = tp + fn
    n_good = tn + fp
    recall = tp / n_bad if n_bad else float("nan")
    specificity = tn / n_good if n_good else float("nan")
    print("\n=== summary ===")
    print(f"  bad  (anomalies): {n_bad:3}  caught={tp}  missed={fn}  -> recall      = {recall:.0%}")
    print(f"  good (clean):     {n_good:3}  passed={tn}  flagged={fp} -> specificity = {specificity:.0%}")
    if err:
        print(f"  errors: {err}")
    print("\n  recall      = fraction of real anomalies routed to human review (want HIGH)")
    print("  specificity = fraction of clean fields correctly left alone (false-alarm cost)")


if __name__ == "__main__":
    main()
