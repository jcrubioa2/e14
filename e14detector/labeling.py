"""Local Claude-assisted labeling of candidate crops -> public seeds.

This is for the FIRST seed pass only. A separate *local* Claude Code session reads the
exported crops and writes a one-word CLEAN/DIRTY label for each; ``label-import`` then
applies those labels to the results DB as seed verdicts. We trust hand/Claude labels
over the cheap screen (Gemma) on precision, which is what matters for seeds.

The live vote-based adjudication is unaffected — it still runs on OpenRouter.

Round trip:
  1. ``e14detector label-export`` -> writes ``review/label_queue.jsonl`` + ``review/LABELING.md``.
  2. Point another Claude Code session at the queue; it reads each ``path`` and writes
     ``review/label_done.jsonl`` (one ``{field_key, label}`` per line).
  3. ``e14detector label-import`` -> applies the labels (DIRTY seeds publicly, CLEAN is
     recorded as confirmed-clean and suppresses any prior screen flag).
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .schemas import FieldClassification
from .storage import DetectorStore
from .vlm.prompt import _RUBRIC

QUEUE_NAME = "label_queue.jsonl"
LABELS_NAME = "label_done.jsonl"
GUIDE_NAME = "LABELING.md"
# A CLEAN verdict needs no DB row and never ships — we only must avoid re-selecting that
# crop. So CLEAN (and DIRTY) crop paths are recorded in this small local registry, which
# label-export excludes. Only DIRTY also writes a seed verdict to the (uploaded) DB.
LABELED_NAME = "labeled_crops.txt"

# DIRTY seeds publicly (the generic "strange" class shown as "señalada para revisar");
# CLEAN is recorded as confirmed-clean (never shown, and overrides a prior screen flag).
_DIRTY_CLASS = FieldClassification.SUSPICIOUS_OVERLAP.value
_CLEAN_CLASS = FieldClassification.CLEAN.value


def _results_db(output_dir: Path) -> Path:
    return Path(output_dir) / "results" / "results.sqlite"


def _field_key(row) -> str:
    return f"{row['document_id']}:{row['page_number']}:{row['row_number']}:{row['section'] or ''}"


def export_label_queue(
    output_dir: Path,
    limit: int | None = None,
    only_flagged: bool = False,
    include_labeled: bool = False,
    department: str | None = None,
    document_id: str | None = None,
    shuffle: bool = False,
    seed: int = 0,
) -> tuple[Path, int]:
    """Write the labeling queue + guide. Returns (queue_path, n_crops).

    With ``document_id`` set, exports every candidate crop of that one acta (regardless of
    prior verdict) — for (re)evaluating a specific table.
    """
    review_dir = Path(output_dir) / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    # Targeting one acta means re-evaluating it, so include already-labeled crops.
    only_unlabeled = not include_labeled and document_id is None
    store = DetectorStore(_results_db(output_dir))
    try:
        rows = list(
            store.candidate_crops_for_labeling(
                only_unlabeled=only_unlabeled,
                only_flagged=only_flagged,
                department=department,
                document_id=document_id,
            )
        )
    finally:
        store.close()
    # Skip crops already labeled (CLEAN lives only in this registry, not the DB) — unless
    # we're deliberately re-evaluating one acta by id.
    if document_id is None:
        registry = labeled_registry(output_dir)
        if registry:
            rows = [r for r in rows if r["raw_crop_path"] not in registry]
    if shuffle:
        random.Random(seed).shuffle(rows)
    if limit is not None:
        rows = rows[:limit]

    queue_path = review_dir / QUEUE_NAME
    with queue_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            rec = {
                "field_key": _field_key(r),
                "document_id": r["document_id"],
                "page": r["page_number"],
                "row": r["row_number"],
                "section": r["section"] or "",
                "candidate": r["candidate_name"] or r["candidate_number"],
                "path": r["raw_crop_path"],
                "label": None,
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    _write_guide(review_dir / GUIDE_NAME, queue_path, review_dir / LABELS_NAME, len(rows))
    return queue_path, len(rows)


def _write_guide(guide_path: Path, queue_path: Path, labels_path: Path, n: int) -> None:
    guide_path.write_text(
        f"""# Labeling task — E-14 vote crops ({n} to label)

You are labeling cropped images of hand-written vote counts from Colombian E-14 forms.
Each one is a single candidate's vote box. Decide **CLEAN** or **DIRTY** for each.

## Definition (use this exactly)

{_RUBRIC}

When unsure, label **CLEAN** — a seed false positive is far more costly than a miss
(real tampering is ~1% of crops, so most are genuinely CLEAN).

## Protocol

1. Read every line of `{queue_path.name}` (JSONL). Each line has a `path` (the crop image)
   and a `field_key` (its stable id).
2. Open each crop with the Read tool and look at it.
3. Append one line per crop to `{labels_path.name}` (JSONL), e.g.:
   `{{"field_key": "<copy from queue>", "label": "CLEAN"}}`
   (Keeping the `path` instead of `field_key` also works.)
4. Label every crop exactly once. Output ONLY CLEAN or DIRTY.

When done, the operator runs: `e14detector label-import --output-dir <dir>`
""",
        encoding="utf-8",
    )


def _resolve(store: DetectorStore, rec: dict) -> tuple[int, str] | None:
    """Match a label record to a candidate row; return (field_id, raw_crop_path)."""
    path = rec.get("path")
    if path:
        fid = store.candidate_id_for_crop(str(path))
        if fid is not None:
            return fid, str(path)
    key = rec.get("field_key")
    if key and str(key).count(":") >= 3:
        document_id, page, row, section = str(key).rsplit(":", 3)
        try:
            r = store.candidate_row_for_key(document_id, int(page), int(row), section)
        except ValueError:
            return None
        if r is not None and r["raw_crop_path"]:
            return r["id"], r["raw_crop_path"]
    return None


def labeled_registry(output_dir: Path) -> set[str]:
    """Crop paths already labeled (CLEAN or DIRTY) — excluded from future exports."""
    reg = Path(output_dir) / "review" / LABELED_NAME
    return set(reg.read_text(encoding="utf-8").split()) if reg.exists() else set()


def import_labels(output_dir: Path, labels_path: Path | None = None) -> dict[str, int]:
    """Apply labels: DIRTY -> seed verdict in the DB; CLEAN -> registry only (not uploaded).

    Both are recorded in the local labeled-crops registry so they aren't re-selected. The
    transient ``label_done.jsonl``/``label_queue.jsonl`` are removed after a clean apply.
    """
    output_dir = Path(output_dir)
    labels = Path(labels_path) if labels_path else output_dir / "review" / LABELS_NAME
    if not labels.exists():
        raise FileNotFoundError(f"labels file not found: {labels}")
    totals = {"dirty": 0, "clean": 0, "skipped": 0, "unmatched": 0}
    registry = output_dir / "review" / LABELED_NAME
    already = labeled_registry(output_dir)
    store = DetectorStore(_results_db(output_dir))
    # The crop run can hold the write lock for a full commit batch (~tens of seconds);
    # wait it out rather than failing this tiny seed write.
    store.conn.execute("PRAGMA busy_timeout=180000")
    try:
        registry.parent.mkdir(parents=True, exist_ok=True)
        with registry.open("a", encoding="utf-8") as reg:
            for line in labels.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                label = str(rec.get("label", "")).strip().upper()
                if label not in ("CLEAN", "DIRTY"):
                    totals["skipped"] += 1
                    continue
                resolved = _resolve(store, rec)
                if resolved is None:
                    totals["unmatched"] += 1
                    continue
                fid, crop_path = resolved
                if label == "DIRTY":
                    raw = json.dumps({"source": "claude-local-label", "label": "DIRTY"})
                    store.set_field_classification(fid, _DIRTY_CLASS, 1.0, raw)
                    totals["dirty"] += 1
                else:
                    totals["clean"] += 1  # CLEAN: no DB row, registry only
                if crop_path not in already:
                    reg.write(crop_path + "\n")
                    already.add(crop_path)
        store.commit()
    finally:
        store.close()
    # Drop the transient batch files once applied (the registry is the durable record).
    if totals["unmatched"] == 0 and totals["skipped"] == 0:
        labels.unlink(missing_ok=True)
        (output_dir / "review" / QUEUE_NAME).unlink(missing_ok=True)
    return totals
