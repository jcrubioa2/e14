# Part 3 — Anomaly review web app

Goal: serve a browsable report of candidate vote-number crops for actas that
contain Qwen-reviewed candidate rows needing attention, with a summary dashboard
and region/severity filtering. Read-only review tool, runs locally or on one VM.

## Status as of 2026-06-01

Implemented and running locally.

- App: `e14detector/webapp.py`
- Templates: `e14detector/templates/dashboard.html`,
  `e14detector/templates/doc.html`
- CLI: `e14detector serve --host --port --output-dir --results`
- Current local URL: `http://127.0.0.1:8001`
- Tests added/updated:
  - `tests/test_webapp.py`
  - `tests/test_vlm_review.py`
  - `tests/test_vlm_json.py`
  - `tests/test_preprocess_features_classifier.py`
- Focused verification at end of session:
  `pytest tests/test_preprocess_features_classifier.py tests/test_vlm_review.py tests/test_vlm_json.py tests/test_webapp.py`
  → `17 passed`

## Locked decisions
- **Visible report set:** a document appears if ≥1 **candidate** field
  (`row_type = 'candidate'`) has Qwen result in:
  `SUSPICIOUS_OVERLAP`, `DIGIT_SHAPE_ANOMALY`, or `UNCLEAR`.
  Rows that Qwen or deterministic cleanup marks `CLEAN` are hidden.
- **Summary rows excluded** from qualifying (votos en blanco / nulos / no
  marcados / total). Shown greyed on the detail page for context only; not
  reviewed by Qwen by default (saves paid calls).
- **Hosting:** dynamic app, local/VM. Stack = FastAPI + Uvicorn + Jinja2.
- **Provider:** Qwen3.6-flash, DashScope intl endpoint (already verified).
- **Public copy:** report pages are Spanish and non-technical. `UNCLEAR` is shown
  as `necesita revision humana`. Avoid words like VLM/CV in the public UI.

## Data / scale facts
- Corpus 121,041 PDFs; ~17 fields each (13 candidate + 4 summary).
- Crop disk per doc: raw field ~0.2 MB, slots ~0.6 MB, debug ~8.3 MB.
  → At scale **keep only raw candidate crops**; do NOT generate debug crops
  (`process` without `--debug`) — debug would be ~1 TB.
- SQLite with ~2M field rows is fine for queries; no DB migration needed.

## Work items

### A. VLM pass: candidates-only + scoped review — DONE
1. `storage.fields_needing_vlm(candidates_only=True)` — DONE (skips summary rows).
2. `vlm_review.run_vlm_review(..., candidates_only=True)` — DONE.
3. `cli vlm-review --include-summary` — DONE; default remains off.
4. `cli vlm-review --document-id DOCUMENT_ID` — DONE; lets us review one acta
   without spending calls on unrelated pending rows.
5. `--min-confidence` remains a report filter, not a review filter.
6. A temporary `--all-candidates` idea was started and then removed. We do **not**
   review all candidate rows by default because it is too slow/expensive.

### B. Crop hygiene for scale — CURRENT DEFAULTS OK
4. Confirm `process` full run uses no `--debug`; only raw + enhanced field crops
   are written. (Slots still written for audit; optional `--no-slots` later.)
5. Retention: long-term we only need raw crops for *qualifying* candidate fields,
   but keep all raw crops for now (~27 GB) — prune later if needed.

### C. Web app (`e14detector/webapp.py` + `cli serve`) — DONE
6. Added deps: `fastapi`, `uvicorn[standard]`, `jinja2`; test dep `httpx`.
7. App reads `results.sqlite` read-only and serves crops from `output_dir`.
8. Routes:
   - `GET /` — Spanish dashboard: totals, visible acta table, filters by
     department, alert type, minimum security/confidence, and free text.
   - `GET /doc/{document_id}` — detail: number-crop for every candidate (flagged
     ones highlighted) with initial alert + final review/confidence/read value;
     summary rows greyed below as context.
   - `GET /crop?path=...` — safe static image serve (path must resolve inside
     `output_dir`; reject traversal). Fixed path resolver so stored paths like
     `data/detector/crops/...` no longer render as broken images.
   - `GET /api/flagged?...` — JSON of the qualifying list (for future tooling).
9. Current qualifying SQL equivalent:
   ```sql
   SELECT d.*, COUNT(*) FILTER (WHERE vf.row_type='candidate'
        AND vf.vlm_classification IN ('SUSPICIOUS_OVERLAP',
                                      'DIGIT_SHAPE_ANOMALY',
                                      'UNCLEAR')
        AND vf.vlm_confidence >= :min_conf) AS n_confirmed
   FROM documents d JOIN vote_fields vf ON vf.document_id=d.document_id
   GROUP BY d.document_id HAVING n_confirmed > 0
   ORDER BY n_confirmed DESC;
   ```
10. `cli serve --host --port --output-dir` → `uvicorn.run(app)`.
11. Templates in `e14detector/templates/` (Jinja2): `dashboard.html`, `doc.html`.
12. Public wording changed to Spanish/non-technical:
    - `necesita revision humana`
    - `numero encima de una raya`
    - `numero con forma rara`
    - `seguridad minima`
    - summary rows labelled `Otros votos`.

### D. Recognition / false positive and false negative changes — DONE

Problem found during manual review:
- A candidate crop like filler mark + clear digits was being shown as
  `necesita revision humana`.
- A crop with only filler marks was being shown as `necesita revision humana`.
- A more suspicious leading mark was missed by the first CV pass, so Qwen never
  reviewed it.

Implemented fixes:
1. Added cheap slot feature `spiky_component_score` in `cv_features.py` using
   contour geometry (solidity/extent/circularity/aspect). This detects crossed
   or star-like marks without sending every candidate to Qwen.
2. Added field-level rule in `classifier.py`:
   `leading_placeholder_digit_ambiguity`.
   This marks a row `UNCLEAR` when the leading mark could be a filler mark or an
   altered digit and the following slots look like digits. It queues Qwen review
   without automatically calling it fraud/suspicious.
3. Added deterministic all-filler cleanup in `classifier.py`:
   if all three slots look like filler/star marks, classify as `CLEAN` with
   reason `all slots appear to contain filler marks`; do not send to Qwen.
4. Updated Qwen prompt generically, with no explicit numeric examples:
   unused leading filler marks are normal; all-placeholder fields are not
   visual anomalies by themselves; read only actual digit marks.
5. Added post-Qwen normalization in `vlm_review.py`:
   - all placeholder text from Qwen can be downgraded to `CLEAN`
   - leading filler + readable digits can be downgraded to `CLEAN`
6. Made VLM JSON parsing more tolerant of JSON embedded in extra model text.

Important: these fixes are conservative. False positives are reduced for clear
filler cases, while the suspicious leading-mark pattern is still routed to
Qwen/human review.

### E. Validation examples processed

Original sample:
- 36 docs, 612 fields.
- Before VLM, dashboard empty because `vlm_classification` was `NULL`.

Added acta 37:
- `E14_PRE_27_004_000_00_001_delegados`
- `27 SANTANDER / 004 AGUADA / Zona 000 / Puesto 00 / Mesa 001`
- `PUESTO CABECERA MUNICIPAL`
- Processed: 17 fields.
- Qwen reviewed one candidate row:
  Abelardo de la Espriella → `UNCLEAR`, read value `139`, confidence `0.60`.
- Appears in report as `necesita revision humana`.

Added acta 38:
- `E14_PRE_29_022_000_01_003_delegados`
- `29 TOLIMA / 022 CAJAMARCA / Zona 000 / Puesto 01 / Mesa 003`
- `IE TEC NTRA SRA DEL ROSARIO SD PPAL`
- Processed: 17 fields.
- After recognition fixes:
  - Ivan Cepeda: leading filler + readable digits → Qwen/normalization `CLEAN`,
    read value `56`; hidden from public alerts.
  - Eduardo Caicedo: all filler marks → CV `CLEAN`; not sent to Qwen; hidden.
  - Abelardo de la Espriella: `leading_placeholder_digit_ambiguity` →
    Qwen `UNCLEAR`, read value `48`, confidence `0.60`; visible as
    `necesita revision humana`.
- Summary row `votos_nulos` was CV-flagged, but still not sent to Qwen and does
  not qualify the acta.

## Full-run sequence (later, when ready)
1. `process --input-dir data/actas --workers 10 --dpi 300` (no --debug).
2. `vlm-review --provider qwen --concurrency N` (candidates-only; cached/resumable).
3. `serve` (or deploy to a VM behind auth if non-local).

## Cost gate (estimate, to confirm before full VLM run)
- CV flags ~15% of candidate fields → ~13 candidates × ~15% ≈ 2 fields/doc →
  ~250k Qwen calls for 121k docs. Confirm qwen3.6-flash per-call price × thinking
  (1200 tok) before committing; the pass is resumable so it can run in budgeted
  batches with `--limit`.
- Re-estimate after the new recognition rules. The new leading-mark rule may
  increase Qwen volume slightly; all-filler cleanup should reduce noise.
- Do not enable any "review all candidates" policy by default.

## Open defaults (override if wanted)
- `total_votos` excluded with the other summary rows.
- Default confidence floor 0.0 (show all visible Qwen non-clean/unclear rows);
  tune after seeing volume.
- Auth: none (local). Add reverse-proxy basic-auth if hosted publicly.

## F. VLM latency optimizations — DONE (2026-06-01)

The review pass was slow; per-call latency was dominated by deep thinking. Four
changes, all env-configurable (defaults in `config.py`, pinned in `.env`):

1. **Concurrency was THE bottleneck.** `make detector-sample` hard-pinned
   `--concurrency 1` (Makefile `QWEN_CONCURRENCY`), serializing every flagged
   row; the pass is network-bound. Default raised to 12. Provider also gained
   exponential backoff on 429/5xx (honours `Retry-After`); `.env`
   `E14_VLM_CONCURRENCY` default 4 → 16 for ad-hoc runs.
2. **Two-tier thinking exists but is OFF for the flagged-row pass**
   (`E14_VLM_TWO_TIER=0`). Logic in `vlm_review._review_one`; provider/protocol/
   mock take an optional `thinking_budget` kwarg; a budget of 0 →
   `enable_thinking:false`. Why off here: the VLM only sees CV-flagged (already
   hard) rows, so the cheap pass rarely resolves them AND a CLEAN from a
   no-thinking pass would never be escalated (false-negative risk on a fraud
   task). Measured per-row at concurrency 1: fast(0)=2.8s, thinking(1200)=5.5s,
   so two-tier on an UNCLEAR row = 8.3s > 5.5s — a net loss. Two-tier is kept for
   a future *all-candidates* pass where most rows are easy.
3. **Single balanced thinking budget**: `E14_QWEN_THINKING_BUDGET` 1200 → 600
   (one call per row; thinking stays on for quality).
4. **Downscale crops**: `_data_uri` shrinks the long edge to
   `E14_QWEN_MAX_IMAGE_PX` via Pillow before base64; falls back to raw bytes on
   failure. Kept at **384** (not 256): a CLAHE+upscale vs raw A/B on 3 real crops
   gave identical verdicts/reads, so image quality is not the accuracy lever, but
   digit-SHAPE anomaly detection is fine-detail so we avoid aggressive shrink.
   (The existing `enhanced_crop` is a binary CV mask — do NOT feed it to the VLM.)

All 31 tests pass. Biggest single speedup for `detector-sample` is the
concurrency bump; run `make detector-sample QWEN_CONCURRENCY=12` (now default).

## Next steps
- Run more manually selected actas through the improved recognizer to spot-check
  false positives/false negatives before a full run.
- Consider adding a public legend explaining:
  - `necesita revision humana` is not an accusation; it means the image was not
    clear enough for automatic dismissal.
  - `Otros votos` are context only.
- Consider adding a private/admin-only view for CV-only rows pending Qwen, so a
  human can see what is queued before paid calls.
- Keep summary rows excluded from Qwen unless explicitly requested with
  `--include-summary`.
