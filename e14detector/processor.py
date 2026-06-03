"""Processing pipeline for local PDFs (sequential and multiprocess)."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .classifier import classify_field, classify_slot
from .cropper import crop_image, save_field_crops, save_page_debug_overlay
from .cv_features import extract_slot_features
from .digit_shape import digit_shape_score, extract_digit_shape_features
from .layout import field_layouts_for_page
from .pdf_render import PdfRenderError, render_pdf_pages
from .preprocess import preprocess_for_features
from .schemas import DocumentMetadata, FieldClassification, SlotClass, VoteField
from .segmentation import adaptive_slot_boxes
from .storage import DetectorStore
from .utils import enrich_metadata_from_index, parse_document_metadata, sha256_file


def _department_from_pdf(pdf: Path, input_dir: Path) -> str | None:
    """DIVIPOL department code from ``{input_dir}/{dep}/…`` layout."""
    try:
        return Path(pdf).relative_to(Path(input_dir)).parts[0].zfill(2)
    except ValueError:
        return None


def _department_in_range(
    dep: str | None,
    *,
    depto: str | None,
    dept_from: str | None,
    dept_to: str | None,
) -> bool:
    if not dep:
        return depto is None and dept_from is None and dept_to is None
    if depto is not None and dep != str(depto).zfill(2):
        return False
    if dept_from is not None and dep < str(dept_from).zfill(2):
        return False
    if dept_to is not None and dep > str(dept_to).zfill(2):
        return False
    return True


def iter_pdfs(
    input_dir: Path,
    limit: int | None = None,
    *,
    depto: str | None = None,
    dept_from: str | None = None,
    dept_to: str | None = None,
) -> list[Path]:
    input_dir = Path(input_dir)
    pdfs = sorted(input_dir.rglob("*.pdf"))
    if depto is not None or dept_from is not None or dept_to is not None:
        pdfs = [
            p
            for p in pdfs
            if _department_in_range(
                _department_from_pdf(p, input_dir),
                depto=depto,
                dept_from=dept_from,
                dept_to=dept_to,
            )
        ]
    return pdfs[:limit] if limit else pdfs


@dataclass
class PdfComputeResult:
    """Everything produced for one PDF, free of any database/store handle so it
    can be returned across a process-pool boundary and persisted by the parent."""

    meta: DocumentMetadata
    status: str  # "done" or "failed"
    field_count: int
    fields: list[tuple[VoteField, dict[str, Any]]] = dataclass_field(default_factory=list)
    errors: list[tuple[str | None, str, str, str]] = dataclass_field(default_factory=list)


def compute_pdf(
    pdf_path: Path, output_dir: Path, dpi: int, debug: bool, crop_only: bool = False
) -> PdfComputeResult:
    """Render, crop and (optionally) classify one PDF. Pure compute + crop writes; no DB.

    Crop PNGs are written here so the heavy Pillow saves stay parallelised; all
    SQLite writes are deferred to the parent process via the returned result.

    ``crop_only`` skips the CV feature extraction / classification entirely (the
    expensive per-slot work). It still saves the crops the public poll needs and
    records neutral classifications (NOT_APPLICABLE). Use it for the fast national
    first pass where Gemma — not CV — is the analyzer.
    """
    pdf_path = Path(pdf_path)
    meta = enrich_metadata_from_index(parse_document_metadata(pdf_path))
    meta = replace(
        meta,
        source_sha256=sha256_file(pdf_path),
        processing_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    fields: list[tuple[VoteField, dict[str, Any]]] = []
    errors: list[tuple[str | None, str, str, str]] = []
    field_count = 0

    try:
        pages = render_pdf_pages(pdf_path, pages=config.DEFAULT_PAGES, dpi=dpi)
        for page in pages:
            if debug:
                save_page_debug_overlay(
                    page.image,
                    page.page_number,
                    Path(output_dir) / "debug" / f"{meta.document_id}_p{page.page_number}_layout.png",
                )
            for layout in field_layouts_for_page(page.page_number, page.width, page.height):
                shape_slot = None
                try:
                    # Threshold the whole field once only to locate ink valleys, then
                    # place slot boundaries there so a drifting digit is not sliced
                    # across a fixed third (see segmentation.adaptive_slot_boxes).
                    # Per-slot features still use the original per-slot preprocessing so
                    # the tuned thresholds stay valid.
                    field_binary = preprocess_for_features(np.array(crop_image(page.image, layout.field_box)))
                    slot_boxes = adaptive_slot_boxes(layout.field_box, field_binary)
                    paths = save_field_crops(page.image, layout, meta.document_id, output_dir, slot_boxes=slot_boxes)
                    if crop_only:
                        # Fast national first pass: keep the crops the poll needs,
                        # skip all CV analysis, leave the verdict to Gemma.
                        slot_paths = [str(path) for path in paths.slot_paths]
                        fields.append((
                            VoteField(
                                document_id=meta.document_id,
                                page_number=page.page_number,
                                row_type=layout.row.row_type,
                                row_number=layout.row.row_number,
                                candidate_number=layout.row.candidate_number,
                                candidate_name=layout.row.candidate_name,
                                section=layout.row.section,
                                raw_crop_path=str(paths.raw_crop_path),
                                enhanced_crop_path=str(paths.enhanced_crop_path),
                                debug_crop_path=str(paths.debug_crop_path),
                                slot_1_crop_path=slot_paths[0],
                                slot_2_crop_path=slot_paths[1],
                                slot_3_crop_path=slot_paths[2],
                                cv_classification=FieldClassification.NOT_APPLICABLE,
                                final_classification=FieldClassification.NOT_APPLICABLE,
                                final_reason="CV disabled (crop-only)",
                                needs_human_review=False,
                            ),
                            {"crop_only": True},
                        ))
                        field_count += 1
                        continue
                    slot_features = []
                    slot_binaries = []
                    for slot_box in slot_boxes:
                        binary = preprocess_for_features(np.array(crop_image(page.image, slot_box)))
                        slot_features.append(extract_slot_features(binary))
                        slot_binaries.append(binary)
                    slot_results = [classify_slot(features) for features in slot_features]
                    # Digit-shape anomaly only applies to slots that actually contain a
                    # digit-like stroke. Blank/placeholder/unclear slots (including margin
                    # border artifacts) must not produce a shape anomaly.
                    shape_scores = [
                        digit_shape_score(extract_digit_shape_features(binary))
                        if result.slot_class == SlotClass.DIGIT
                        else 0.0
                        for binary, result in zip(slot_binaries, slot_results)
                    ]
                    max_shape_score = max(shape_scores, default=0.0)
                    shape_slot = (shape_scores.index(max_shape_score) + 1) if max_shape_score >= 0.65 else None
                    final = classify_field(slot_features, digit_shape_score=max_shape_score, shape_anomaly_slot=shape_slot)
                    raw_crop_path = str(paths.raw_crop_path)
                    enhanced_crop_path = str(paths.enhanced_crop_path)
                    debug_crop_path = str(paths.debug_crop_path)
                    slot_paths = [str(path) for path in paths.slot_paths]
                    features_payload = {"slots": [features.to_json() for features in slot_features]}
                except Exception as exc:
                    final = classify_field([], crop_failed=True)
                    slot_results = []
                    raw_crop_path = None
                    enhanced_crop_path = None
                    debug_crop_path = None
                    slot_paths = [None, None, None]
                    features_payload = {"error": str(exc)}
                    errors.append((meta.document_id, str(pdf_path), "CROP_FAILED", str(exc)))
                    shape_slot = None
                field = VoteField(
                    document_id=meta.document_id,
                    page_number=page.page_number,
                    row_type=layout.row.row_type,
                    row_number=layout.row.row_number,
                    candidate_number=layout.row.candidate_number,
                    candidate_name=layout.row.candidate_name,
                    section=layout.row.section,
                    raw_crop_path=raw_crop_path,
                    enhanced_crop_path=enhanced_crop_path,
                    debug_crop_path=debug_crop_path,
                    slot_1_crop_path=slot_paths[0],
                    slot_2_crop_path=slot_paths[1],
                    slot_3_crop_path=slot_paths[2],
                    slot_1_class=slot_results[0].slot_class if slot_results else SlotClass.UNCLEAR,
                    slot_2_class=slot_results[1].slot_class if slot_results else SlotClass.UNCLEAR,
                    slot_3_class=slot_results[2].slot_class if slot_results else SlotClass.UNCLEAR,
                    cv_classification=final.final_classification,
                    cv_score=final.cv_score,
                    placeholder_overlap_score=final.placeholder_overlap_score,
                    digit_shape_score=final.digit_shape_score,
                    shape_anomaly_slot=shape_slot,
                    final_classification=final.final_classification,
                    final_reason=final.reason,
                    anomaly_tags=list(final.anomaly_tags),
                    needs_human_review=final.needs_human_review,
                )
                fields.append((field, features_payload))
                field_count += 1
        return PdfComputeResult(meta=meta, status="done", field_count=field_count, fields=fields, errors=errors)
    except PdfRenderError as exc:
        errors.append((meta.document_id, str(pdf_path), "PDF_RENDER_FAILED", str(exc)))
    except Exception as exc:
        errors.append((meta.document_id, str(pdf_path), "UNKNOWN_ERROR", str(exc)))
    return PdfComputeResult(meta=meta, status="failed", field_count=field_count, fields=fields, errors=errors)


def _persist(store: DetectorStore, result: PdfComputeResult) -> None:
    store.clear_document_results(result.meta.document_id)
    store.upsert_document(result.meta)
    for field, features in result.fields:
        store.insert_vote_field(field, features=features)
    for document_id, source_path, code, message in result.errors:
        store.insert_error(document_id, source_path, code, message)


def _counts(result: PdfComputeResult) -> dict[str, int]:
    return {
        "done": 1 if result.status == "done" else 0,
        "skipped": 0,
        "failed": 1 if result.status == "failed" else 0,
        "fields": result.field_count,
    }


def process_pdf(pdf_path: Path, output_dir: Path, store: DetectorStore, dpi: int, debug: bool, force: bool = False, crop_only: bool = False) -> dict[str, int]:
    meta = enrich_metadata_from_index(parse_document_metadata(pdf_path))
    if not force and store.already_processed(meta.document_id, sha256_file(pdf_path)):
        return {"done": 0, "skipped": 1, "failed": 0, "fields": 0}
    result = compute_pdf(pdf_path, output_dir, dpi=dpi, debug=debug, crop_only=crop_only)
    _persist(store, result)
    return _counts(result)


def run_process_one(pdf_path: Path, output_dir: Path, dpi: int, debug: bool, force: bool = True) -> dict[str, int]:
    """Process exactly one PDF. Convenience path for fast single-document iteration."""
    config.ensure_output_dirs(output_dir)
    store = DetectorStore(Path(output_dir) / "results" / "results.sqlite", Path(output_dir) / "results" / "results.jsonl")
    try:
        result = process_pdf(Path(pdf_path), output_dir, store, dpi=dpi, debug=debug, force=force)
        store.commit()
    finally:
        store.close()
    return result


def _compute_worker(args: tuple[str, str, int, bool, bool]) -> PdfComputeResult:
    pdf_path, output_dir, dpi, debug, crop_only = args
    return compute_pdf(Path(pdf_path), Path(output_dir), dpi=dpi, debug=debug, crop_only=crop_only)


def _max_inflight(workers: int) -> int:
    """Cap queued PDF jobs so we never materialize tens of thousands of Futures at once.

    The old path submitted the entire national ``todo`` list up front (~80k+ pending
    tasks). That ballooned parent RAM and made the pool fragile under memory pressure
    (workers dying → CPU \"winding down\"). Keep a small sliding window instead.
    """
    return min(512, max(64, workers * 4))


def _run_pool_bounded(
    executor: ProcessPoolExecutor,
    todo: list[Path],
    output_dir: Path,
    store: DetectorStore,
    dpi: int,
    debug: bool,
    crop_only: bool,
    totals: dict[str, int],
    max_inflight: int,
) -> None:
    pdf_iter = iter(todo)
    inflight: dict = {}
    done = 0

    def submit_more() -> None:
        while len(inflight) < max_inflight:
            try:
                pdf = next(pdf_iter)
            except StopIteration:
                return
            fut = executor.submit(
                _compute_worker,
                (str(pdf), str(output_dir), dpi, debug, crop_only),
            )
            inflight[fut] = pdf

    submit_more()
    while inflight:
        finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
        for fut in finished:
            inflight.pop(fut, None)
            result = fut.result()
            _persist(store, result)
            counts = _counts(result)
            for key in totals:
                totals[key] += counts[key]
            done += 1
            if done % 50 == 0:
                store.commit()
                print(
                    f"  processed {done}/{len(todo)} (skipped {totals['skipped']})",
                    flush=True,
                )
        submit_more()
    store.commit()


def run_process(
    input_dir: Path,
    output_dir: Path,
    limit: int | None,
    dpi: int,
    debug: bool,
    force: bool = False,
    workers: int = 1,
    crop_only: bool = False,
    depto: str | None = None,
    dept_from: str | None = None,
    dept_to: str | None = None,
) -> dict[str, int]:
    config.ensure_output_dirs(output_dir)
    store = DetectorStore(Path(output_dir) / "results" / "results.sqlite", Path(output_dir) / "results" / "results.jsonl")
    totals = {"done": 0, "skipped": 0, "failed": 0, "fields": 0}
    pdfs = iter_pdfs(
        input_dir, limit=limit, depto=depto, dept_from=dept_from, dept_to=dept_to
    )
    if depto is not None or dept_from is not None or dept_to is not None:
        span = depto or f"{dept_from or '00'}-{dept_to or '99'}"
        print(f"  dept filter: {span} ({len(pdfs)} PDFs in slice)", flush=True)

    try:
        if workers and workers > 1:
            # Resume is presence-based here (skip documents already in the DB) to
            # avoid hashing every file on the hot path; --force reprocesses all.
            already = set()
            if not force:
                already = {row[0] for row in store.conn.execute("SELECT document_id FROM documents")}
            todo = []
            for pdf in pdfs:
                document_id = parse_document_metadata(pdf).document_id
                if not force and document_id in already:
                    totals["skipped"] += 1
                else:
                    todo.append(pdf)

            inflight_cap = _max_inflight(workers)
            print(
                f"  pool: {workers} workers, max {inflight_cap} in-flight "
                f"({len(todo)} actas to process)",
                flush=True,
            )
            with ProcessPoolExecutor(max_workers=workers) as executor:
                _run_pool_bounded(
                    executor,
                    todo,
                    output_dir,
                    store,
                    dpi,
                    debug,
                    crop_only,
                    totals,
                    inflight_cap,
                )
        else:
            for pdf in pdfs:
                result = process_pdf(pdf, output_dir, store, dpi=dpi, debug=debug, force=force, crop_only=crop_only)
                for key in totals:
                    totals[key] += result[key]
                store.commit()
    finally:
        store.close()
    return totals
