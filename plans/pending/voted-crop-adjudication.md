# Plan: harden the voted-crop adjudication (poll CONFIRM step)

**Status:** draft for deliberation · **Owner:** TBD · **Date:** 2026-06-02

## Goal

Improve the VLM decision that runs when the community flags a candidate crop ("marcar como
extraño") and the poll reaches threshold. We want to **reliably separate genuinely tampered
crops from honest ones — especially the hard sub-pixel cases** (e.g. a digit written on top of
a placeholder mark) — at acceptable cost and latency, using **a single preserved run** if
possible.

**Out of scope (settled):** the seed SCREEN pass. That stays **one pass of `gemma-4-31b`**,
starting at ~500 actas. Don't redesign seeding here. See [[e14-vlm-validation-2026-06]].

## Why this is hard (empirical findings, 2026-06-02)

Probe scripts: `scripts/test_dirty_prompt.py` (model × resolution sweep) and
`scripts/gemma_precision.py` (per-model precision on a 39-crop local set). Canonical hard case:
`E14_PRE_01_001_013_04_015` candidate row 4 — reads "131" where the first digit is written over
a placeholder stroke ("the 131 case"). Clean controls: rows 3 & 5 of the same acta (plain
placeholder dots), which a good model must keep CLEAN.

Results with the user's DIRTY-rubric prompt (bare CLEAN/DIRTY), temp 0:

| model | catches 131 (×8) | clean dots stay CLEAN? | DIRTY-rate on 39 crops | cost in/out per M |
|---|---|---|---|---|
| `gemini-2.5-flash-lite` | **8/8** | **no** (FPs both) | **82%** (rubber-stamp) | $0.10 / $0.40 |
| `gemma-4-31b-it` | 2/8 (unstable) | **yes** | 10% (4/39, all plausibly anomalous) | $0.12 / $0.37 |
| `gemma-3-27b-it` | 5/8 (noisy) | no | high | $0.08 / $0.16 |
| `qwen3-vl-8b/32b`, `qwen2.5-72b`, `qwen3.6-flash`, `gemini-2.5-flash` | ~0/8 | yes | ~0% (blind) | varies |
| `anthropic/claude-haiku-4.5` (current live model) | only at 768px | no (FPs dots) | high at 768px | $1 / $5 |

**Key takeaways:**
1. **Temp-0 is NOT deterministic here** — gemma flipped 131 between CLEAN/DIRTY run to run. Any
   single-shot conclusion is noise; measure with N votes.
2. **There's a precision/recall wall.** Models that catch 131 (`gemini-2.5-flash-lite`) do so by
   leaning DIRTY on almost everything (82% — useless). Models that stay calm on clean dots
   (`gemma-4-31b`) can't reliably see 131 (2/8). No single (model × prompt × res) does both.
3. **`gemma-4-31b`'s DIRTY calls are *meaningful*** — the 4 it flagged on the broader set were
   genuinely anomalous (digit-over-asterisk `✱A8`, overdrawn `5`, overlapping asterisks), not the
   clean dots. It's a real discriminator; it just has low recall on the subtlest overlaps.
4. **Resolution is a hyperparameter.** 131 only flips DIRTY at 768px/full — but at 768px the
   clean placeholder-dot crops start false-positiving too. Live default is
   `E14_QWEN_MAX_IMAGE_PX=256` (`.env` had 384). See [[e14-vlm-validation-2026-06]].
5. **The current live poll model is Haiku** (`E14_OPENROUTER_MODEL=anthropic/claude-haiku-4.5`)
   — both the worst on this case *and* the most expensive (~10–25× the alternatives). Strong
   candidate to replace regardless of which strategy wins.

## The adjudication context matters (changes the math)

The CONFIRM step runs **only on crops a human already flagged**. So:
- **The base rate of true-dirty among flagged crops is much higher than the ~1% global rate**
  ([[e14-gold-label-set]]) — humans pre-filter. Recall matters more here than in seeding.
- **But CONFIRM must still be able to DEMOTE a false flag back to CLEAN** (and the appeal path
  "Se ve normal" depends on it). A rubber-stamp-DIRTY model (gemini-flash-lite at 82%) is
  useless: it would confirm every flag and never demote → the poll becomes "whatever the loudest
  voters said." So we need **discrimination**, not just recall.
- Hysteresis already exists: `E14_POLL_RESCALE_STEP=5`, "clean is re-eligible." A wrong CONFIRM
  isn't permanent, but it's public-facing while it stands.

## Strategies to evaluate

The user proposed three; treat them as a matrix, not either/or.

### A. Ensemble of two models (mix verdicts)
Run 2 complementary models and combine. Natural pairing from the data: a **high-recall** model
(catches subtle overlaps) + a **high-precision** model (won't rubber-stamp).
- Combination rules to test: AND (both DIRTY → DIRTY; conservative, high precision), OR (either
  DIRTY → DIRTY; high recall), or weighted/tie-break by a third call.
- Candidate pair: `gemma-4-31b` (precision anchor) + something that actually sees 131. The
  problem: the only thing that saw 131 reliably was the rubber-stamp `gemini-flash-lite`, so OR
  collapses to "always DIRTY." Need to find a genuine high-recall-but-discriminating model
  (re-test `qwen3-vl-32b`/`30b-thinking` and newer `gemini-3.x-flash` at higher res).
- Cost: 2× calls. Still cheaper than current Haiku if both are cheap models.

### B. Self-consistency: multiple calls to ONE model, majority/threshold vote
Exploits finding #1 (non-determinism). Run the same model K times (temp > 0) and threshold.
- `gemma-4-31b` on 131 was 2/8 DIRTY, ~10% FP on clean. Model the tradeoff:
  - "≥1 of 3 → DIRTY": recall on 131 ≈ 1−(6/8)³ ≈ **58%**, but FP on clean ≈ 1−0.9³ ≈ **27%**.
  - "≥2 of 3 → DIRTY": recall drops to ~16%, FP ~3%.
  - Neither is clearly good — **quantify on a real labeled set before judging.** The 2/8 and
    ~10% are tiny samples; real per-call p may differ.
- Cost: K× calls of one model.

### C. Single call, thinking enabled, capped (the user's preference: "preserve just 1 run")
Enable the model's reasoning with a bounded budget and keep one shot.
- Infra already exists: `VLM_TWO_TIER`, `thinking_budget`, `QWEN_ESCALATE_THINKING_BUDGET`,
  `enable_thinking`/`thinking_budget` in `alibaba_qwen_provider.py`. The two-tier path
  (cheap pass → escalate UNCLEAR to a thinking pass) is the closest existing pattern.
- Test: does a capped thinking budget (e.g. 300–1200 tok) let a *discriminating* model
  (`gemma`/`qwen3-vl-*-thinking`/`qwen3.6-flash`) reason its way to the 131 overlap WITHOUT
  the rubber-stamp behavior? The preferred VLM per [[e14-vlm-qwen-model]] is Qwen3.6-Flash with
  thinking ≤1200 tok — **this strategy is the reason that note exists; it's untested on the
  poll path.** Prioritize it.
- Cost: 1 call but more output tokens (thinking). Often cheaper than B/A and lower latency than B.

### Cross-cutting levers (apply to all three)
- **Resolution**: sweep 256 / 384 / 512 / 768 as a parameter. 131 needs >256; clean dots break
  at 768. There may be a sweet spot (~384–512) — or a two-resolution ensemble.
- **Prompt design**: the bare DIRTY-rubric prompt pushes models toward DIRTY. Test a
  *calibrated* prompt that explicitly describes the CLEAN placeholder convention (dots/dashes/
  asterisks in empty slots are NORMAL) so the model's "anomaly" bar is the overlap, not "any
  mark." Compare against the existing skeptical `VOTE_FIELD_CONFIRM_PROMPT`.
- **Crop variant**: raw vs enhanced crop (`raw_crop_path` vs `enhanced_crop_path`) — the
  enhanced/preprocessed version may make overlaps more legible.

## Evaluation methodology (do this FIRST — it gates everything)

The session could not locate a stored gold label set; **building a proper labeled eval set is
the prerequisite.** Without it, every result above is anecdote.

1. **Assemble a labeled adjudication set** that reflects the *flagged-crop* distribution, not the
   global one: oversample genuinely-anomalous crops + known-clean placeholder crops + ambiguous
   ones (the 131 class). Target ≥150 crops, human-labeled (Claude-assisted labeling exists, see
   `e14detector/labeling.py` and [[e14-gold-label-set]]). Include the canonical 131 and the
   `✱A8` / overdrawn-`5` cases already identified.
2. **Metrics** (report both, this is a 2-objective problem):
   - **Recall on true-DIRTY** (catch tampering) — the headline for a transparency tool.
   - **Demotion correctness on true-CLEAN flags** (= precision of the CONFIRM=DIRTY decision):
     fraction of honest crops correctly sent back to CLEAN. A rubber-stamp scores ~0 here.
   - Secondary: cost per adjudication, p50/p95 latency, stability (variance across repeats).
3. **Decision rule**: pick the cheapest strategy on the recall/demotion Pareto front that clears
   a threshold both objectives (propose ≥0.85 demotion-correctness so the poll isn't a
   rubber-stamp; maximize recall subject to that). Numbers to be agreed with the user.
4. Reuse/extend the probe scripts; promote them into a proper `scripts/eval_adjudication.py`
   that takes a labeled CSV and emits the metrics table.

## Concrete experiment matrix (for the deliberating agent)

For each cell, run on the labeled set, report (recall, demotion-correctness, $/crop, latency):

- **Models**: `gemma-4-31b`, `qwen3-vl-32b-instruct`, `qwen3-vl-30b-a3b-thinking`,
  `qwen3.6-flash` (thinking), `gemini-3.1-flash-lite`, `gemini-3-flash-preview`. (Drop
  `gemini-2.5-flash-lite` unless calibrated-prompt fixes its 82% rate; drop Haiku on cost.)
- **Strategies**: (A) best AND-pair, best OR-pair; (B) K∈{3,5} self-consistency on the top
  single model; (C) single call + thinking budget ∈ {0, 300, 1200}.
- **Resolution**: {256, 384, 512, 768}.
- **Prompt**: {existing CONFIRM, calibrated-convention, bare-rubric (baseline)}.

Don't run the full cross-product blindly — start with single-call × resolution × prompt to find
the best base config, THEN layer ensemble/self-consistency/thinking on the winner.

## Wiring notes (where things live)
- Live model: `config.OPENROUTER_MODEL` (`E14_OPENROUTER_MODEL`, fly.toml). Screen model:
  `E14_SCREEN_MODEL`. Resolution: `config.QWEN_MAX_IMAGE_PX` (`E14_QWEN_MAX_IMAGE_PX`).
- Adjudication entry points: `webapp.adjudicate` / `adjudicate_appeal` (poll path),
  `vlm_review.run_seed_confirm` (seed CONFIRM), `_review_one` (two-tier logic +
  `_normalize_placeholder_result` placeholder downgrade).
- Prompts: `e14detector/vlm/prompt.py` (`VOTE_FIELD_REVIEW_PROMPT`, `VOTE_FIELD_CONFIRM_PROMPT`).
- Provider thinking controls: `e14detector/vlm/alibaba_qwen_provider.py`.
- Admin already lets the operator re-run a model on a crop (`/admin/review`, probar/aplicar) —
  useful for spot-checking the chosen config in production. The CONFIRM prompt is env-overridable
  (`E14_CONFIRM_PROMPT`) — **note: a neutral prompt may currently be set as a Fly secret; reconcile
  before measuring** (see [[e14-vlm-validation-2026-06]]).

## Open questions for discussion
1. Is "preserve 1 run" a hard constraint (cost/latency) or a preference? It rules out B and
   constrains A — worth confirming, since the data hints a single discriminating model may simply
   not exist for the 131 class.
2. Acceptable cost/latency budget per adjudication? (Sets how many calls we can afford.)
3. Is the 131-class (sub-pixel overlap) even in-scope for the VLM, or is it explicitly delegated
   to the crowd poll + human appeal? Today's data suggests it may be below model acuity at
   safe resolutions — the honest answer might be "humans handle this tier."
4. Target thresholds for recall vs demotion-correctness?
