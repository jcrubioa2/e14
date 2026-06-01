# Codex Project Prompt: E-14 Vote-Count Visual Anomaly Detector

## Project Context

We have a local collection of approximately 120k PDF files. Each PDF is an E-14 election form scan with handwritten vote-count summaries.

The goal is to build a local Python pipeline that detects **possible visual anomalies** in handwritten vote-count fields.

The system must be conservative. It should not claim fraud, tampering, forgery, or intent. It should only flag visual cases for human verification.

Use language such as:

* “possible visual anomaly”
* “suspicious overlap”
* “digit-shape anomaly”
* “needs human review”
* “unclear mark”

Avoid language such as:

* “fraud”
* “fake”
* “forged”
* “tampered”
* “dirty game confirmed”

## Local Runtime Environment

The project will run locally inside **WSL**.

Available machine:

```text
RAM: 32 GB
CPU: Xeon E5-2620 v3, modified/tuned around 3.2 GHz
GPU: AMD RX 480, 4 GB VRAM
Environment: WSL
```

## Hardware Strategy

The MVP should be designed as a **CPU-first, streaming pipeline**.

The available AMD RX 480 GPU may be used if it is accessible from WSL and if it provides a real benefit, but the project must not depend on GPU acceleration.

The first version must work fully with:

* CPU PDF rendering
* CPU OpenCV preprocessing
* CPU feature extraction
* CPU heuristic classification
* optional external VLM API calls

The GPU must be treated as:

```text
optional
runtime-detected
non-required
safe to disable
```

Do not require CUDA, ROCm, DirectML, OpenCL, or any GPU-specific stack for the MVP.

## Optional GPU Acceleration Policy

Add a configuration flag:

```text
E14_USE_GPU=auto
```

Allowed values:

```text
off
auto
opencl
directml
rocm
```

Default:

```text
auto
```

Expected behavior:

```text
off:
  Never use GPU.

auto:
  Try to detect usable acceleration.
  If unavailable or unstable, fall back to CPU without failing the pipeline.

opencl:
  Try OpenCV/OpenCL acceleration if cv2.ocl is available.
  Fall back to CPU if unavailable.

directml:
  Reserved for later ML-based classifier experiments.
  Do not require for MVP.

rocm:
  Reserved for later experiments only if WSL + AMD driver + GPU support is confirmed.
  Do not require for MVP.
```

For the MVP, only implement `off` and `auto` cleanly. `opencl`, `directml`, and `rocm` can be stubs or future options if not immediately useful.

The project should include a diagnostic command:

```bash
python -m e14detector.cli env-check
```

This should print:

```text
Python version
OS / WSL detection
CPU count
Available RAM if easy to detect
OpenCV version
OpenCV OpenCL available: true/false
OpenCV OpenCL enabled: true/false
GPU mode requested
GPU mode actually used
PDF rendering backend available
Output directory write test
```

If GPU detection fails, print a warning but continue.

Example:

```text
GPU acceleration requested: auto
OpenCV OpenCL available: false
Using CPU pipeline.
```

## WSL-Specific Requirements

Because the project runs inside WSL:

1. Use Linux-compatible paths internally.
2. Accept input/output paths from CLI parameters.
3. Avoid hardcoded Windows paths.
4. Support `/mnt/c/...` paths.
5. Make the output directory configurable.
6. Do not use GUI display windows such as `cv2.imshow`.
7. Save debug images to disk instead of opening visual windows.
8. Keep all file writes explicit and predictable.
9. Make worker count configurable.
10. Use conservative default worker counts to avoid memory spikes.
11. Document how to run the CLI from WSL.

Example command:

```bash
python -m e14detector.cli process \
  --input-dir /mnt/c/path/to/e14_pdfs \
  --output-dir ./data/output \
  --limit 20 \
  --workers 4 \
  --dpi 300 \
  --vlm-mode off
```

## Performance Strategy for This Machine

The available machine has enough CPU and RAM for the MVP as long as processing is streaming and memory-conscious.

Suggested defaults:

```text
DPI: 300
workers: 4
vlm_mode: off
gpu_mode: auto
save_full_pages: false
save_debug_overlays: true for small test runs, false for full batch
batch_size: streaming / one PDF at a time per worker
```

For small test runs:

```bash
python -m e14detector.cli process \
  --input-dir /mnt/c/path/to/sample_pdfs \
  --output-dir ./data/output \
  --limit 50 \
  --workers 4 \
  --dpi 300 \
  --vlm-mode off \
  --gpu-mode auto \
  --debug
```

For larger runs:

```bash
python -m e14detector.cli process \
  --input-dir /mnt/c/path/to/all_pdfs \
  --output-dir ./data/output \
  --workers 4 \
  --dpi 300 \
  --vlm-mode suspicious-only \
  --gpu-mode auto
```

## Memory Rules

32 GB RAM is enough if the pipeline avoids holding many rendered pages in memory.

Implementation rules:

* Do not load all PDFs into memory.
* Do not accumulate rendered page images in lists.
* Do not accumulate all crops in memory before writing.
* Process one PDF/page at a time per worker.
* Write crop images incrementally.
* Write results incrementally.
* Commit SQLite transactions in batches.
* Release image arrays after each page.
* Avoid storing full rendered pages unless debug mode is enabled.
* Keep debug output disabled for full-scale runs unless explicitly requested.

## GPU Guidance

The AMD RX 480 has only 4 GB VRAM and may or may not be usable from WSL depending on drivers and supported acceleration stack.

Do not attempt local VLM/LLM inference on this GPU in the MVP.

The GPU may be useful only for limited image-processing or later small classifier experiments, but the first version should not depend on it.

The local GPU does not matter for Alibaba/Qwen VLM inference because that is external API-based.

The pipeline should do cheap local CV filtering first, then send only selected crops to the VLM provider.

## Form Structure

Each PDF usually has 3 pages.

Relevant pages for the first version:

* **Page 1:** candidate rows 1–7 and the right-side `VOTACIÓN` column.
* **Page 2:** candidate rows 8–13 plus summary rows such as blank votes, null votes, unmarked votes, and total.
* **Page 3:** mostly juror notes/signatures; not part of the initial visual anomaly detector.

Each vote-count field has up to **three visual slots**, because vote counts are at most three digits.

When a value has fewer than three digits, unused slots may contain placeholder marks such as:

* dot
* middle dot
* dash
* asterisk-like mark
* small filler mark
* blank-like filler

## Main Objective

Build a Python-based local processing pipeline that:

1. Reads local E-14 PDF files.
2. Renders relevant pages as images.
3. Locates and crops vote-count regions.
4. Splits vote-count fields into three slots.
5. Detects possible visual anomalies using deterministic computer vision.
6. Optionally sends suspicious/unclear crops to a vision LLM through a provider abstraction, initially intended for Alibaba Cloud / Qwen.
7. Stores structured results, metadata, crop paths, debug images, model outputs, and audit information.
8. Produces a reviewable output that lets users locate the official document elsewhere.

We do **not** need to publicly serve the PDFs or page images. The official government website can serve the original documents. Our system only needs enough metadata to help users locate the relevant official document/table.

## Anomaly Types

The system should support at least two visual anomaly classes.

### 1. Placeholder Overlap Anomaly

Classification value:

```text
SUSPICIOUS_OVERLAP
```

This is for cases where a digit appears written on top of, overlapping, replacing, or visually merging with a placeholder mark.

Examples:

* A slot contains both a placeholder-like dot/dash and digit-like stroke.
* A digit appears to be written directly over a filler mark.
* A placeholder and digit occupy the same slot in a visually suspicious way.
* Stroke density or connected components suggest multiple marks in the same slot.

This class is about **digit-placeholder interaction**.

### 2. Digit-Shape / Possible Alteration Anomaly

Classification value:

```text
DIGIT_SHAPE_ANOMALY
```

This is for subtler cases where a digit does not necessarily overlap a placeholder, but looks visually inconsistent compared with other digits in the same document, same vote column, same row, or nearby rows.

Example pattern:

* A vote field reads as a valid number, such as `139`.
* The leading `1` looks unusual, slash-like, overwritten, too angled, too short, too thin, too thick, or morphologically different.
* Another nearby `1`, such as in a value like `51`, looks clearly different and more natural.
* The suspicious digit may look added later or altered, but the system must not infer intent.

This class is about **digit morphology inconsistency**.

Use `DIGIT_SHAPE_ANOMALY` only when the difference is meaningful. Use `UNCLEAR` when it could be ordinary handwriting variation.

## Classification Values

Use these final classifications:

```text
CLEAN
SUSPICIOUS_OVERLAP
DIGIT_SHAPE_ANOMALY
UNCLEAR
CROP_FAILED
NOT_APPLICABLE
```

A field may also have multiple anomaly tags:

```json
{
  "final_classification": "DIGIT_SHAPE_ANOMALY",
  "anomaly_tags": [
    "digit_shape_inconsistency",
    "possible_leading_digit_alteration"
  ]
}
```

Recommended slot-level classes:

```text
DIGIT
PLACEHOLDER
BLANK
MIXED
UNCLEAR
```

## First Implementation Goal

Do not build the full final product at once.

Start with an MVP that can process a small folder of PDFs and produce:

* cropped vote-count images
* enhanced crop images
* debug overlay images showing crop boxes and slot boundaries
* JSONL or SQLite results
* one row of output per vote field
* conservative classification per field
* optional VLM review only for suspicious/unclear cases

The first success criterion is:

```text
Reliably crop the correct visual areas and produce inspectable outputs.
```

Perfect anomaly detection is not required in the first pass.

## Suggested Tech Stack

Use Python.

Recommended libraries:

```text
PyMuPDF / fitz
opencv-python
numpy
Pillow
pydantic
sqlite3 or duckdb
tqdm
typer or click
pytest
```

Optional later:

```text
streamlit
scikit-image
scikit-learn
torch / torchvision
onnxruntime
```

No GPU should be required for the MVP.

## Proposed Repository Structure

```text
e14-anomaly-detector/
  README.md
  pyproject.toml
  .env.example

  src/
    e14detector/
      __init__.py
      cli.py
      config.py
      schemas.py

      env_check.py
      pdf_render.py
      layout.py
      cropper.py
      preprocess.py

      cv_accel.py
      cv_features.py
      classifier.py
      digit_shape.py
      comparison.py

      vlm/
        __init__.py
        base.py
        mock_provider.py
        alibaba_qwen_provider.py
        prompt.py

      storage.py
      official_locator.py
      review_export.py
      utils.py

  tests/
    test_env_check.py
    test_layout.py
    test_preprocess.py
    test_classifier.py
    test_digit_shape.py
    test_storage.py
    test_vlm_json.py

  data/
    input/
    output/
      crops/
      slots/
      debug/
      results/
      review/
```

## CLI Requirements

Implement a CLI similar to:

```bash
python -m e14detector.cli process \
  --input-dir ./data/input \
  --output-dir ./data/output \
  --limit 20 \
  --workers 4 \
  --dpi 300 \
  --vlm-mode off \
  --gpu-mode auto
```

Useful additional commands:

```bash
python -m e14detector.cli env-check
```

```bash
python -m e14detector.cli inspect-layout \
  --pdf ./data/input/example.pdf \
  --output-dir ./data/output/debug \
  --dpi 300 \
  --gpu-mode auto
```

```bash
python -m e14detector.cli review-export \
  --results ./data/output/results/results.sqlite \
  --output ./data/output/review/review_cases.csv
```

The process should be resumable.

If a PDF has already been processed and the source file hash has not changed, skip it unless `--force` is passed.

## Data Model

Use Pydantic schemas.

### DocumentMetadata

Fields:

```text
document_id
source_path
source_sha256
filename
department_code
department_name
municipality_code
municipality_name
zone
puesto
mesa
place_name
official_lookup_url
metadata_confidence
metadata_source
processing_timestamp
```

For the first version, parse what is reliably available from the filename. Allow unknown fields to be null.

### VoteField

Fields:

```text
document_id
page_number
row_type
row_number
candidate_number
candidate_name
section

raw_crop_path
enhanced_crop_path
debug_crop_path

slot_1_crop_path
slot_2_crop_path
slot_3_crop_path

read_value

slot_1_class
slot_2_class
slot_3_class

cv_classification
cv_score
placeholder_overlap_score
digit_shape_score

shape_anomaly_slot
shape_anomaly_digit
comparison_crop_path
comparison_notes

vlm_classification
vlm_confidence
vlm_raw_json

final_classification
final_reason
anomaly_tags
needs_human_review
```

### CVFeatures

Store inspectable feature values as JSON, including:

```text
ink_density
component_count
largest_component_area
component_bounding_boxes
aspect_ratios
stroke_width_estimate
slot_density
slot_component_count
placeholder_like_component_count
digit_like_component_count
mixed_component_score
slant_angle
height_width_ratio
skeleton_length
endpoint_count
relative_darkness
relative_size
```

### RuntimeInfo

Store runtime information per batch:

```text
run_id
start_timestamp
end_timestamp
input_dir
output_dir
dpi
workers
vlm_mode
gpu_mode_requested
gpu_mode_used
opencv_version
opencl_available
opencl_enabled
wsl_detected
python_version
status
```

## Processing Pipeline

### 1. Environment Check

Before processing, initialize runtime configuration.

Implement:

```python
detect_wsl()
detect_opencv_opencl()
configure_gpu_mode(gpu_mode: str)
```

Expected behavior:

* If `gpu_mode=off`, disable optional GPU paths.
* If `gpu_mode=auto`, try safe acceleration only.
* If acceleration is unavailable, use CPU.
* Never fail the pipeline only because GPU acceleration is unavailable.

OpenCV OpenCL detection can use:

```python
cv2.ocl.haveOpenCL()
cv2.ocl.useOpenCL()
cv2.ocl.setUseOpenCL(True)
```

Only use OpenCL-accelerated `UMat` operations if benchmarks or tests show they are stable and beneficial. Otherwise, keep CPU NumPy arrays.

### 2. PDF Rendering

Render only pages 1 and 2 by default.

Use configurable DPI. Default:

```text
300 DPI
```

Do not persist full rendered pages unless debug mode is enabled.

Process streaming-style:

1. Open PDF.
2. Render relevant page.
3. Crop relevant regions.
4. Save crops/results.
5. Release page image.
6. Continue.

### 3. Page Layout and Alignment

Start with fixed normalized coordinates because the E-14 layout is regular.

Implement a layout module that maps normalized crop coordinates to rendered image pixels.

The first version should support:

* page 1 vote column crop
* page 2 vote column crop
* row-level vote field crops
* slot-level crops inside each vote field

Later improvements can add:

* deskew correction
* border detection
* black bar detection
* horizontal line detection
* template matching
* layout confidence score

For the MVP, make crop boxes easy to tune.

Create debug overlay images showing:

* page boundary
* vote column boundary
* row crop boundaries
* slot boundaries

### 4. Cropping

For each relevant page:

1. Crop the right-side `VOTACIÓN` column.
2. Crop each vote field row.
3. Split each vote field into three slots.
4. Save raw crop.
5. Save enhanced crop.
6. Save optional slot crops.
7. Save debug crop with slot boundaries.

Suggested naming convention:

```text
{document_id}_p{page}_row{row_number}_{row_type}_field.png
{document_id}_p{page}_row{row_number}_{row_type}_field_enhanced.png
{document_id}_p{page}_row{row_number}_{row_type}_slot1.png
{document_id}_p{page}_row{row_number}_{row_type}_slot2.png
{document_id}_p{page}_row{row_number}_{row_type}_slot3.png
{document_id}_p{page}_row{row_number}_{row_type}_debug.png
```

### 5. Preprocessing

Create preprocessing functions for:

* grayscale conversion
* contrast normalization
* adaptive thresholding
* denoising
* inversion if needed
* connected component extraction
* morphological open/close operations
* optional skeletonization

Always preserve the raw crop. Enhanced crops are for analysis and review only.

Preprocessing should accept and return normal NumPy arrays by default.

Optional acceleration should be isolated in `cv_accel.py`, not spread throughout the codebase.

### 6. Placeholder Overlap Detection

For each slot and field, compute features such as:

* ink density
* number of connected components
* largest component area
* bounding box size
* component height
* component width
* whether small placeholder-like marks and digit-like strokes coexist
* whether components merge unnaturally
* whether the slot has abnormal ink density compared with neighboring slots
* whether the mark crosses the expected placeholder area

Suggested heuristic:

```text
If a slot contains one small compact component:
  likely PLACEHOLDER

If a slot contains one large/tall component:
  likely DIGIT

If a slot contains both a small placeholder-like component and a digit-like component:
  slot class = MIXED
  increase placeholder_overlap_score

If a component is unusually dense, merged, or visually crowded:
  increase placeholder_overlap_score or mark UNCLEAR
```

Classification guidance:

```text
low placeholder_overlap_score -> CLEAN
medium score -> UNCLEAR
high score -> SUSPICIOUS_OVERLAP
```

Prefer `UNCLEAR` over overclaiming.

### 7. Digit-Shape Anomaly Detection

Add separate modules:

```text
digit_shape.py
comparison.py
```

This module should detect unusual digit morphology, especially suspicious-looking leading digits.

For each digit-like slot, compute:

```text
bounding box width
bounding box height
aspect ratio
stroke density
skeleton length
slant angle
center of mass
number of connected components
endpoint count after skeletonization
local stroke thickness
darkness relative to nearby digits
size relative to nearby digits
whether the digit is slash-like
whether the digit has retracing or double strokes
```

For the digit `1`, pay special attention to:

```text
verticality / slant angle
height-to-width ratio
top hook
base stroke
slash-like appearance
unusual shortness
unusual thinness
unusual darkness
unusual angle
```

### 8. Intra-Document Digit Comparison

Digit-shape anomaly detection should compare a suspicious digit with other examples of the same digit when possible.

Comparison priority:

1. same vote field
2. same row
3. same page
4. same `VOTACIÓN` column
5. same document
6. summary rows
7. nearby fields

Example:

If a field reads `139`, isolate the leading `1` and compare it to other visible `1`s in the same document, such as from values like `51`, `11`, `101`, `215`, etc.

Do not assume all digits were written by the same person. The comparison should produce a soft anomaly score, not a definitive conclusion.

Suggested scoring:

```text
digit_shape_score =
  unusual_slant_score
  + unusual_size_score
  + unusual_density_score
  + mismatch_against_same_digit_examples
  + overwrite_or_retrace_score
  + local_context_suspicion_score
```

Classification guidance:

```text
low digit_shape_score -> CLEAN
medium digit_shape_score -> UNCLEAR
high digit_shape_score -> DIGIT_SHAPE_ANOMALY
```

### 9. Field-Level Final Classification

Combine signals conservatively.

Suggested logic:

```text
If crop failed:
  final_classification = CROP_FAILED

Else if placeholder_overlap_score is high:
  final_classification = SUSPICIOUS_OVERLAP

Else if digit_shape_score is high:
  final_classification = DIGIT_SHAPE_ANOMALY

Else if either score is medium or image quality is poor:
  final_classification = UNCLEAR

Else:
  final_classification = CLEAN
```

Set:

```text
needs_human_review = true
```

for:

```text
SUSPICIOUS_OVERLAP
DIGIT_SHAPE_ANOMALY
UNCLEAR
```

### 10. Optional Vision LLM Review

Create a provider abstraction.

Do not hardcode Alibaba/Qwen logic into the pipeline.

Suggested interface:

```python
class VisionReviewer:
    def review_vote_field(self, image_paths: list[str], metadata: dict) -> VLMReviewResult:
        ...
```

Implement:

```text
MockVisionReviewer
AlibabaQwenVisionReviewer
```

Read VLM config from environment variables:

```text
VLM_ENABLED=false
VLM_PROVIDER=alibaba_qwen
VLM_API_KEY=
VLM_BASE_URL=
VLM_MODEL=
VLM_TIMEOUT_SECONDS=60
VLM_MAX_RETRIES=3
```

If the exact Alibaba API shape is not known yet, create a clean adapter with TODOs and keep mock mode working.

Only call the VLM when:

```text
--vlm-mode on
```

or:

```text
--vlm-mode suspicious-only
```

and CV returns:

```text
UNCLEAR
SUSPICIOUS_OVERLAP
DIGIT_SHAPE_ANOMALY
```

Cache VLM results by image hash so reruns do not repeat paid API calls.

### 11. VLM Prompt

Use a strict prompt. The model should inspect crops, not make political claims.

Prompt:

```text
You are inspecting a cropped handwritten vote-count field from an election form.

The field has exactly three slots because the value can be up to three digits.

When a slot is unused, it may contain a placeholder mark such as:
- dot / middle dot
- dash
- asterisk-like mark
- small filler mark

Task:
Determine whether the crop shows any visual anomaly.

Inspect for two anomaly types:

1. Placeholder overlap:
A digit appears written on top of, overlapping, replacing, or visually merging with a placeholder mark.

2. Digit-shape anomaly:
A digit appears visually inconsistent, slash-like, overwritten, retraced, unusually angled, unusually sized, or meaningfully different from comparable digits in the same crop or comparison image.

Pay special attention to:
- leading digits in 3-digit values
- the digit 1
- digits that look slash-like
- digits that appear added in a different style
- digits with double strokes, retracing, or abnormal density

Classify the crop as one of:
- CLEAN: digits and placeholders are visually separate and normal.
- SUSPICIOUS_OVERLAP: a digit appears to overlap a placeholder, or there is both a placeholder-like mark and digit-like stroke in the same slot.
- DIGIT_SHAPE_ANOMALY: a digit has a meaningful visual inconsistency compared with nearby or provided comparison digits.
- UNCLEAR: image quality is insufficient or the mark is ambiguous.

Use UNCLEAR if the difference could reasonably be normal handwriting variation.

Do not claim fraud, tampering, forgery, or intent.

Return strict JSON only:
{
  "classification": "CLEAN | SUSPICIOUS_OVERLAP | DIGIT_SHAPE_ANOMALY | UNCLEAR",
  "read_value": "...",
  "slot_analysis": [
    {
      "slot": 1,
      "content": "digit | placeholder | blank | unclear | mixed",
      "read_as": "...",
      "shape_anomaly": true,
      "overlap_anomaly": false,
      "notes": "..."
    },
    {
      "slot": 2,
      "content": "digit | placeholder | blank | unclear | mixed",
      "read_as": "...",
      "shape_anomaly": false,
      "overlap_anomaly": false,
      "notes": "..."
    },
    {
      "slot": 3,
      "content": "digit | placeholder | blank | unclear | mixed",
      "read_as": "...",
      "shape_anomaly": false,
      "overlap_anomaly": false,
      "notes": "..."
    }
  ],
  "comparison_used": true,
  "comparison_notes": "Briefly compare suspicious digits with similar digits if a comparison image was provided.",
  "reason": "Brief visual reason only. Do not infer fraud beyond the crop."
}
```

Pass metadata alongside the image:

```json
{
  "document_id": "...",
  "page": 1,
  "row_type": "candidate",
  "row_number": 4,
  "candidate_name": "...",
  "expected_format": "three slots, max 3 digits, unused slots may contain placeholders"
}
```

When reviewing digit-shape anomalies, send:

* suspicious field crop
* enhanced suspicious field crop
* isolated suspicious digit crop
* comparison crop with other examples of similar digits from the same document, if available

### 12. Storage

Use SQLite for the MVP.

Tables:

```text
documents
vote_fields
cv_features
digit_comparisons
vlm_reviews
processing_errors
runtime_runs
```

Also export JSONL.

Every result must be traceable to:

```text
original PDF path
source file hash
page number
row number
slot number when applicable
crop path
classification reason
processing timestamp
runtime settings
```

### 13. Official Document Locator

Do not host PDFs publicly.

Create `official_locator.py` to generate locator metadata that helps users find the document on the official government site.

For now, generate a locator object:

```json
{
  "department_code": "...",
  "municipality_code": "...",
  "zone": "...",
  "puesto": "...",
  "mesa": "...",
  "page": 1,
  "row_number": 4,
  "candidate_name": "...",
  "instructions": "Open the official government E-14 lookup page and search using these values."
}
```

Later, add a real URL builder if the official URL pattern is stable.

### 14. Review Export

Generate a CSV or HTML review export with one row per suspicious/unclear case.

Fields:

```text
document_id
department_code
department_name
municipality_code
municipality_name
zone
puesto
mesa
page_number
row_type
row_number
candidate_name
read_value

final_classification
final_reason
anomaly_tags

placeholder_overlap_score
digit_shape_score
shape_anomaly_slot
shape_anomaly_digit

cv_score
vlm_classification
vlm_confidence
comparison_notes

raw_crop_path
enhanced_crop_path
debug_crop_path
comparison_crop_path

official_lookup_url
official_lookup_instructions
```

For suspicious digit-shape cases, the review packet should include:

* raw field crop
* enhanced field crop
* isolated suspicious digit crop
* comparison crop with similar digits from the same document
* debug image showing the suspicious slot
* metadata needed to locate the official form

### 15. Error Handling

The system must not stop because of one bad PDF.

Catch and store errors such as:

```text
PDF_RENDER_FAILED
PAGE_MISSING
LAYOUT_FAILED
CROP_FAILED
LOW_LAYOUT_CONFIDENCE
VLM_API_ERROR
VLM_INVALID_JSON
STORAGE_ERROR
GPU_INIT_FAILED
GPU_UNAVAILABLE
UNKNOWN_ERROR
```

Continue processing the next PDF.

GPU-related failures must be warnings, not fatal errors, unless the user explicitly requested a strict GPU-only mode in the future.

### 16. Performance Requirements

The CV pipeline should run on normal CPU hardware.

Do not keep all page images in memory.

Process streaming-style.

Add multiprocessing or concurrent workers for local CV processing.

Keep VLM calls:

* optional
* cached
* retryable
* rate-limited
* limited to unclear/suspicious cases

Suggested worker defaults for this machine:

```text
test/debug runs: 2 to 4 workers
larger CPU runs: start with 4 workers, increase only after observing RAM and disk behavior
VLM calls: separate rate-limited queue
```

Do not mix heavy VLM API calls directly into high-parallel PDF workers without rate limits.

### 17. Tests

Implement tests for:

```text
environment / WSL detection
GPU mode fallback
filename/document id parsing
coordinate scaling
crop-box generation
preprocessing output shape
connected component feature extraction
placeholder-overlap heuristic
digit-shape heuristic
comparison crop generation
SQLite insert/read
VLM JSON parsing
error handling
```

Use generated synthetic images for classifier tests. Do not require real election PDFs in unit tests.

### 18. Acceptance Criteria for MVP

The MVP is acceptable when:

1. The CLI processes a folder of PDFs.
2. Page 1 and page 2 are rendered.
3. Vote-field crops are created for candidate and summary rows.
4. Debug overlay images show correct crop boxes and slot boundaries.
5. Each crop receives a conservative classification.
6. Placeholder overlap and digit-shape anomaly are separate concepts.
7. Results are written to SQLite and JSONL.
8. The process is resumable.
9. The pipeline runs with `--vlm-mode off`.
10. The pipeline runs with `--gpu-mode off`.
11. The pipeline runs with `--gpu-mode auto` and falls back to CPU if no usable GPU acceleration exists.
12. VLM integration is optional and behind a provider abstraction.
13. Suspicious/unclear cases are exported for human review.
14. Errors are logged per document without stopping the batch.

## Implementation Strategy

Work incrementally.

### Step 1

Create project structure, config, schemas, and CLI skeleton.

### Step 2

Implement `env-check`, WSL detection, and GPU mode fallback.

### Step 3

Implement PDF rendering and debug output.

### Step 4

Implement fixed-coordinate cropper for pages 1 and 2.

### Step 5

Implement vote-field and slot crop saving.

### Step 6

Implement preprocessing and basic CV features.

### Step 7

Implement placeholder-overlap heuristic classifier.

### Step 8

Implement digit-shape feature extraction and comparison logic.

### Step 9

Implement final conservative classification logic.

### Step 10

Implement SQLite and JSONL storage.

### Step 11

Implement mock VLM provider and prompt.

### Step 12

Add Alibaba/Qwen provider adapter, but keep it optional.

### Step 13

Add review export.

At each step, keep the code runnable and add tests.

## Important Design Principles

* Be conservative.
* Do not claim fraud.
* Preserve raw crops.
* Store hashes and metadata for traceability.
* Make crop/debug inspection easy.
* Keep VLM calls optional, cached, and limited to uncertain cases.
* Avoid storing full rendered pages unless debugging.
* Prefer simple, reliable CV before adding complex ML.
* Make layout parameters configurable because scanned forms may shift.
* Optimize for auditability over cleverness.
* Separate placeholder-overlap anomalies from digit-shape anomalies.
* Use human review for anything potentially consequential.
* Design CPU-first.
* Treat GPU acceleration as optional and runtime-detected.
* Never let unavailable GPU acceleration block the core pipeline.
