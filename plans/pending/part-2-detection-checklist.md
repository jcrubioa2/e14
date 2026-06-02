# E-14 Detector — implementation checklist

Goal: extend the current scraper project with a local, CPU-first visual anomaly detector for E-14 vote-count fields. Do not claim fraud or intent; only flag possible visual anomalies for human review.

## 0. Repo Integration
- [x] Keep existing `e14/` scraper modules unchanged unless a detector read-only helper is clearly needed.
- [x] Add a new package `e14detector/`.
- [x] Update `pyproject.toml` package list and dependencies.
- [x] Add detector CLI entry point support via `python -m e14detector.cli`.
- [x] Add detector output directories under `data/detector/` by default.
- [x] Add tests under `tests/`.

## 1. Project Skeleton
- [x] Create `e14detector/__init__.py`.
- [x] Create `e14detector/cli.py`.
- [x] Create `e14detector/config.py`.
- [x] Create `e14detector/schemas.py`.
- [x] Create `e14detector/utils.py`.
- [x] Define enums for field classifications: `CLEAN`, `SUSPICIOUS_OVERLAP`, `DIGIT_SHAPE_ANOMALY`, `UNCLEAR`, `CROP_FAILED`, `NOT_APPLICABLE`.
- [x] Define slot classes: `DIGIT`, `PLACEHOLDER`, `BLANK`, `MIXED`, `UNCLEAR`.

## 2. Environment Check
- [x] Create `e14detector/env_check.py`.
- [x] Implement WSL detection.
- [x] Implement CPU count detection.
- [x] Implement best-effort RAM detection.
- [x] Implement OpenCV version and OpenCL availability detection.
- [x] Implement `E14_USE_GPU` / `--gpu-mode` handling with `off` and `auto`.
- [x] Ensure unavailable GPU/OpenCL prints a warning and falls back to CPU.
- [x] Add CLI command: `python -m e14detector.cli env-check`.
- [x] Test GPU fallback does not fail on CPU-only environments.

## 3. PDF Rendering
- [x] Create `e14detector/pdf_render.py`.
- [x] Use PyMuPDF / `fitz` to render pages 1 and 2.
- [x] Make DPI configurable, default `300`.
- [x] Do not persist full rendered pages unless `--debug` is enabled.
- [x] Stream one document/page at a time per worker.
- [x] Catch render errors and record `PDF_RENDER_FAILED` or `PAGE_MISSING`.
- [x] Add unit tests for page selection and render failure handling using generated/minimal PDFs where practical.

## 4. Layout Model
- [x] Create `e14detector/layout.py`.
- [x] Define fixed normalized coordinates for page 1 vote column and page 2 vote column.
- [x] Define row crop boxes for page 1 candidate rows 1-7.
- [x] Define row crop boxes for page 2 candidate rows 8-13 and summary rows.
- [x] Define three slot boxes inside each vote field.
- [x] Make layout constants easy to tune from code/config.
- [x] Add layout confidence placeholder field, defaulting to fixed-coordinate confidence.
- [x] Add tests for normalized-to-pixel coordinate scaling.

## 5. Cropping And Debug Output
- [x] Create `e14detector/cropper.py`.
- [x] Save raw field crops.
- [x] Save enhanced field crops.
- [x] Save slot crops for slots 1, 2, and 3.
- [x] Save debug crop images with slot boundaries.
- [x] Save page-level debug overlays when `--debug` is enabled.
- [x] Use deterministic filenames based on `document_id`, page, row, and row type.
- [x] Ensure crop failure produces `CROP_FAILED` and does not stop the batch.

## 6. Preprocessing
- [x] Create `e14detector/preprocess.py`.
- [x] Implement grayscale conversion.
- [x] Implement contrast normalization.
- [x] Implement adaptive thresholding.
- [x] Implement denoising.
- [x] Implement optional inversion.
- [x] Implement connected component extraction.
- [x] Implement morphological open/close helpers.
- [x] Keep raw crops unchanged; enhanced crops are derived artifacts only.
- [x] Add tests for output shape, dtype, and stable behavior on synthetic images.

## 7. CV Features
- [x] Create `e14detector/cv_features.py`.
- [x] Extract ink density.
- [x] Extract component count and bounding boxes.
- [x] Extract largest component area.
- [x] Extract aspect ratios.
- [x] Extract slot density.
- [x] Extract slot component counts.
- [x] Extract placeholder-like and digit-like component counts.
- [x] Extract mixed component score.
- [x] Add best-effort slant, relative darkness, and relative size features.
- [x] Store feature values as JSON-serializable dictionaries.
- [x] Add synthetic-image tests for placeholder-like, digit-like, blank, and mixed slots.

## 8. Placeholder Overlap Classification
- [x] Create `e14detector/classifier.py`.
- [x] Classify each slot as `DIGIT`, `PLACEHOLDER`, `BLANK`, `MIXED`, or `UNCLEAR`.
- [x] Compute `placeholder_overlap_score`.
- [x] Classify high overlap as `SUSPICIOUS_OVERLAP`.
- [x] Classify medium/ambiguous overlap as `UNCLEAR`.
- [x] Prefer `UNCLEAR` over overclaiming.
- [x] Add tests for clean digits, placeholders, blank slots, mixed digit-placeholder slots, and noisy ambiguous slots.

## 9. Digit Shape Analysis
- [x] Create `e14detector/digit_shape.py`.
- [x] Compute digit morphology features for digit-like slots.
- [x] Focus first on leading digit and slash-like `1` anomalies.
- [x] Compute size, density, aspect ratio, slant, connected components, and retrace/darkness proxies.
- [x] Create `e14detector/comparison.py`.
- [x] Compare suspicious digits against other same-document candidate examples when available.
- [x] Compute `digit_shape_score`.
- [x] Classify high score as `DIGIT_SHAPE_ANOMALY`, medium score as `UNCLEAR`.
- [x] Add synthetic tests for obvious slash-like/outlier digit shapes.

## 10. Final Classification
- [x] Combine crop status, placeholder overlap score, digit shape score, and image quality.
- [x] Priority order: `CROP_FAILED`, `SUSPICIOUS_OVERLAP`, `DIGIT_SHAPE_ANOMALY`, `UNCLEAR`, `CLEAN`.
- [x] Set `needs_human_review=true` for suspicious, anomaly, and unclear cases.
- [x] Store `final_reason` using conservative language only.
- [x] Store anomaly tags such as `placeholder_overlap`, `digit_shape_inconsistency`, and `possible_leading_digit_alteration`.

## 11. Storage
- [x] Create `e14detector/storage.py`.
- [x] Use SQLite under `data/detector/results/results.sqlite` by default.
- [x] Create tables: `documents`, `vote_fields`, `cv_features`, `digit_comparisons`, `vlm_reviews`, `processing_errors`, `runtime_runs`.
- [x] Export JSONL incrementally alongside SQLite.
- [x] Hash each source PDF and skip already processed unchanged documents unless `--force` is passed.
- [x] Commit in batches to avoid large memory buildup.
- [x] Add tests for insert/read/resume behavior.

## 12. Metadata And Official Locator
- [x] Parse document metadata from scraper filenames like `E14_PRE_09_079_099_05_003_delegados.pdf`.
- [x] Allow unknown department/municipality names to be null.
- [x] Use `data/index.csv` when available to enrich names, place name, and official URL.
- [x] Create `e14detector/official_locator.py`.
- [x] Generate locator metadata with codes, page, row, candidate/summary info, and instructions.
- [x] Do not host or copy official PDFs into review exports.

## 13. VLM Provider Abstraction
- [x] Create `e14detector/vlm/base.py`.
- [x] Create `e14detector/vlm/mock_provider.py`.
- [x] Create `e14detector/vlm/prompt.py`.
- [x] Create `e14detector/vlm/alibaba_qwen_provider.py` as optional adapter.
- [ ] Support `--vlm-mode off|on|suspicious-only`, default `off`.
- [ ] Only call VLM for suspicious/unclear fields when mode is `suspicious-only`.
- [ ] Cache VLM results by crop image hash.
- [x] Parse strict JSON results and map invalid responses to `VLM_INVALID_JSON`.
- [x] Add tests for mock provider and VLM JSON parsing.

## 14. Processing CLI
- [x] Implement `process` command.
- [x] Required/important args: `--input-dir`, `--output-dir`, `--limit`, `--workers`, `--dpi`, `--vlm-mode`, `--gpu-mode`, `--debug`, `--force`.
- [x] Default input should be `data/actas` if present.
- [x] Default output should be `data/detector`.
- [x] Process PDFs recursively.
- [x] Keep worker default conservative, initially `4`.
- [x] Continue processing after per-document errors.
- [x] Print summary counts at end.
- [x] Add `inspect-layout` command for one PDF with debug overlays.
- [x] Add `review-export` command.

## 15. Review Export
- [x] Create `e14detector/review_export.py`.
- [x] Export suspicious, digit-shape anomaly, and unclear cases to CSV.
- [x] Include crop paths, scores, final reason, metadata, and official locator fields.
- [x] Include comparison crop path where available.
- [x] Keep export human-review oriented and avoid fraud/tampering language.

## 16. Tests And Acceptance
- [x] Add `pytest` to dev/test dependencies.
- [x] Add tests for env detection and GPU fallback.
- [x] Add tests for filename parsing.
- [x] Add tests for coordinate scaling and crop generation.
- [x] Add tests for preprocessing.
- [x] Add tests for CV feature extraction.
- [x] Add tests for placeholder overlap heuristic.
- [x] Add tests for digit-shape heuristic.
- [x] Add tests for SQLite storage and resume.
- [x] Add tests for VLM JSON parsing.
- [x] Verify `python -m e14detector.cli env-check`.
- [x] Verify `python -m e14detector.cli inspect-layout --pdf <sample> --output-dir data/detector/debug --dpi 300 --gpu-mode off`.
- [x] Verify `python -m e14detector.cli process --input-dir data/actas --output-dir data/detector --limit 20 --workers 4 --dpi 300 --vlm-mode off --gpu-mode off --debug`.
- [x] Verify rerunning the same process skips unchanged PDFs.
- [x] Verify `review-export` writes suspicious/unclear cases.

## Implementation Notes

- Add dependencies to `pyproject.toml`: `PyMuPDF`, `opencv-python`, `numpy`, `Pillow`, `pydantic`, `pytest`; keep VLM HTTP dependencies optional until the Alibaba adapter is wired.
- Use `data/detector/` as the detector output root: `crops/`, `slots/`, `debug/`, `results/results.sqlite`, `results/results.jsonl`, and `review/review_cases.csv`.
- Keep detector state separate from scraper state. Do not reuse `data/manifest.db` for detector results.
- Use `data/index.csv` opportunistically for names and official URLs, but do not require it for MVP processing.
- Treat `data/actas/**/*.pdf` as the default input source because this repo already contains downloaded actas.
- The first MVP acceptance target is crop correctness and inspectable outputs, not perfect anomaly detection.
- Manual acceptance should review debug overlays from `inspect-layout` before trusting full-batch classifications.
