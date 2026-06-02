"""Second-opinion VLM pass over CV-flagged vote fields.

Runs *after* the CV processing pass: it selects fields the heuristics marked
``needs_human_review`` (and that have no verdict yet), sends each crop to the
configured VLM provider, and writes the verdict back. The pass is:

* idempotent / resumable — identical crops are cached by content hash, and only
  rows with ``vlm_classification IS NULL`` are picked up, so it can be re-run or
  interrupted freely without re-billing;
* separated from CV compute — network latency never stalls the CPU render pool,
  and an ordinary ``process`` run can never accidentally call the paid API.
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config
from .schemas import FieldClassification
from .storage import DetectorStore
from .vlm.base import VisionReviewer
from .vlm.factory import build_reviewer

PLACEHOLDER_READ_CHARS = set("*xX×.-_ ")


def _looks_like_placeholder_text(value: str | None) -> bool:
    text = (value or "").strip()
    return bool(text) and all(char in PLACEHOLDER_READ_CHARS for char in text)


def _normalize_placeholder_result(row, result):
    """Downgrade obvious placeholder conventions that the VLM may mark unclear."""
    slot_classes = [row["slot_1_class"], row["slot_2_class"], row["slot_3_class"]]
    if result.classification != FieldClassification.UNCLEAR:
        return result
    if _looks_like_placeholder_text(result.read_value):
        from .vlm.base import VLMReviewResult

        raw = dict(result.raw_json)
        raw["normalization"] = "all_placeholder_marks_clean"
        return VLMReviewResult(
            classification=FieldClassification.CLEAN,
            confidence=max(result.confidence, 0.80),
            read_value=None,
            raw_json=raw,
            reason="all slots appear to contain filler marks, not a visual anomaly",
        )
    if (
        slot_classes[0] in {"PLACEHOLDER", "UNCLEAR"}
        and slot_classes[1] == "DIGIT"
        and slot_classes[2] == "DIGIT"
        and result.read_value
        and result.read_value.strip().isdigit()
    ):
        from .vlm.base import VLMReviewResult

        raw = dict(result.raw_json)
        raw["normalization"] = "leading_placeholder_with_digits_clean"
        return VLMReviewResult(
            classification=FieldClassification.CLEAN,
            confidence=max(result.confidence, 0.80),
            read_value=result.read_value,
            raw_json=raw,
            reason="leading filler mark with readable digits is expected",
        )
    return result


def _crop_hash(path: str | None) -> str | None:
    if not path or not Path(path).exists():
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _review_one(reviewer: VisionReviewer, row) -> dict:
    """Pure network/compute step for one field; returns a persistable record.

    Two-tier review: the first pass runs cheap (thinking disabled). Only rows the
    fast pass leaves UNCLEAR are re-reviewed with the larger thinking budget, so
    the expensive reasoning is spent exclusively on genuinely ambiguous crops.
    """
    image_path = row["raw_crop_path"] or row["enhanced_crop_path"] or row["debug_crop_path"]
    image_hash = _crop_hash(image_path)
    metadata = {
        "candidate_name": row["candidate_name"],
        "page": row["page_number"],
        "row_number": row["row_number"],
        "cv_classification": row["final_classification"],
    }
    images = [p for p in (image_path,) if p]
    if config.VLM_TWO_TIER:
        result = reviewer.review_vote_field(images, metadata, thinking_budget=0)
        result = _normalize_placeholder_result(row, result)
        if result.classification == FieldClassification.UNCLEAR:
            escalated = reviewer.review_vote_field(
                images, metadata, thinking_budget=config.QWEN_ESCALATE_THINKING_BUDGET
            )
            result = _normalize_placeholder_result(row, escalated)
    else:
        result = reviewer.review_vote_field(images, metadata)
        result = _normalize_placeholder_result(row, result)
    return {
        "field_id": row["id"],
        "document_id": row["document_id"],
        "image_hash": image_hash or "",
        "classification": result.classification.value,
        "confidence": result.confidence,
        "read_value": result.read_value,
        "raw_json": json.dumps(result.raw_json, ensure_ascii=False),
    }


def run_vlm_review(
    output_dir: Path,
    provider: str | None = None,
    limit: int | None = None,
    concurrency: int | None = None,
    candidates_only: bool = True,
    document_id: str | None = None,
    verbose: bool = True,
    require_flag: bool = True,
    sample_rate: float | None = None,
) -> dict[str, int]:
    """Review pending fields with the configured VLM provider.

    With ``sample_rate`` set, the pass ignores the CV flag and reviews *every*
    candidate in a deterministic ``sample_rate`` subset of documents — the
    "drop CV, Gemma on N%% of files" mode.
    """
    store = DetectorStore(Path(output_dir) / "results" / "results.sqlite")
    # The proactive pre-screen wants the fast/cheap screen model; only override the
    # model when the resolved provider is OpenRouter (qwen/mock keep their own model).
    resolved = (provider or config.VLM_PROVIDER or "mock").lower()
    if resolved == "openrouter":
        reviewer = build_reviewer(provider, model=config.SCREEN_MODEL, max_tokens=config.SCREEN_MAX_TOKENS)
    else:
        reviewer = build_reviewer(provider)
    workers = concurrency or config.VLM_CONCURRENCY
    totals = {"reviewed": 0, "cached": 0, "failed": 0}

    try:
        document_ids = None
        if sample_rate is not None:
            document_ids = store.sampled_document_ids(sample_rate)
            require_flag = False  # CV not run in this mode; review all sampled candidates
            if verbose:
                print(
                    f"vlm-review: Gemma sample {sample_rate:.0%} -> {len(document_ids)} document(s)",
                    flush=True,
                )
        rows = store.fields_needing_vlm(
            limit=limit,
            candidates_only=candidates_only,
            document_id=document_id,
            require_flag=require_flag,
            document_ids=document_ids,
        )
        if verbose:
            scope = f" document_id={document_id}" if document_id else ""
            row_scope = "candidate rows" if candidates_only else "all rows"
            print(
                f"vlm-review: selected {len(rows)} pending {row_scope}{scope} "
                f"(provider={provider or config.VLM_PROVIDER}, concurrency={workers})",
                flush=True,
            )
        pending = []
        for checked, row in enumerate(rows, start=1):
            # Cache hit: an identical crop was reviewed before — reuse its verdict
            # without another API call.
            image_path = row["raw_crop_path"] or row["enhanced_crop_path"] or row["debug_crop_path"]
            image_hash = _crop_hash(image_path)
            cached = store.vlm_cache_get(image_hash) if image_hash else None
            if cached:
                payload = json.loads(cached["raw_json"]) if cached["raw_json"] else {}
                store.record_vlm_review(
                    field_id=row["id"],
                    document_id=row["document_id"],
                    image_hash=image_hash,
                    classification=cached["classification"],
                    confidence=cached["confidence"],
                    read_value=payload.get("read_value"),
                    raw_json=cached["raw_json"] or "{}",
                )
                totals["cached"] += 1
                if verbose:
                    print(
                        f"vlm-review: cache {totals['cached']}/{checked} "
                        f"{row['document_id']} row {row['row_number']}",
                        flush=True,
                    )
            else:
                pending.append(row)
        store.commit()
        if verbose:
            print(
                f"vlm-review: {totals['cached']} cache hit(s), {len(pending)} API review(s) to run",
                flush=True,
            )
        if not pending:
            return totals

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_review_one, reviewer, row): row for row in pending}
            for done, future in enumerate(as_completed(futures), start=1):
                row = futures[future]
                try:
                    rec = future.result()
                except Exception as exc:
                    store.insert_error(row["document_id"], "", "VLM_REVIEW_FAILED", str(exc))
                    totals["failed"] += 1
                    if verbose:
                        print(
                            f"vlm-review: failed {done}/{len(pending)} "
                            f"{row['document_id']} row {row['row_number']}: {exc}",
                            flush=True,
                        )
                    continue
                store.record_vlm_review(**rec)
                totals["reviewed"] += 1
                if verbose:
                    print(
                        f"vlm-review: reviewed {done}/{len(pending)} "
                        f"{row['document_id']} row {row['row_number']} -> "
                        f"{rec['classification']} ({rec['confidence']:.2f})",
                        flush=True,
                    )
                if done % 50 == 0:
                    store.commit()
                    if verbose:
                        print(
                            f"vlm-review: committed {done}/{len(pending)} "
                            f"(reviewed={totals['reviewed']} failed={totals['failed']})",
                            flush=True,
                        )
        store.commit()
    finally:
        store.close()
    return totals
