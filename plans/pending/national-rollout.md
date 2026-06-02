# E-14 National Rollout — Resume Checklist

_Last updated: 2026-06-02. Branch: `feature/community-poll` (local only, not pushed)._

Goal: take the working 40-acta pilot to the **full 121,913-acta national universe**, deployed
**progressively** (usable from hour 0, fills in over ~18h — no big-bang launch).

---

## ✅ Already done (live)

- **Pilot is live** at `https://e14-poll.fly.dev` (Fly.io, single always-on machine, app `e14-poll`).
- **Community poll** works: crowd flags a crop → at threshold a VLM adjudicates → publishes "strange".
  - Suspicious flow + **appeal flow** ("Se ve normal") both wired; "clean/strange re-eligible" hysteresis.
- **Bot protection = in-app** (honeypot + HMAC-signed, timestamped form token). **Turnstile fully removed**
  (it can't work on a `*.fly.dev` domain; we have no owned domain — `e14-poll.com` is NOT registered).
- **Models by role** (all OpenRouter):
  - Pre-screen / seeds: `google/gemma-4-31b-it` + neutral SCREEN prompt (precision > gemini-lite).
  - Live poll (upvote/downvote): `qwen/qwen3-vl-8b-thinking` (CONFIRM / APPEAL prompts).
  - Verdict is a **bare CLEAN/DIRTY word** (confidence dropped — proven no signal).
- **Gold label set**: `data/detector/review/claude_labels.jsonl` — 104 crops, **103 CLEAN / 1 DIRTY**.
  Key finding: **real fraud base rate ≈ 1%** → seeds must be RARE; precision >> recall.

## 🆕 Public rollout-progress banner (done locally, not yet deployed)

- `/browse` now shows a Spanish sync banner: **"X de 121.913 actas · Z%"**, a progress bar,
  **"Última actualización hace …"**, and **"Tiempo restante estimado ~…"** (hidden when the rate
  sample is non-representative, i.e. ETA > 14 días — so the 40-acta pilot shows no bogus ETA).
- Derived **purely from the served results DB** (`compute_sync_progress` in `webapp.py`): synced =
  distinct actas with a candidate crop; last sync = `MAX(processing_timestamp)`; ETA from the
  timestamp span. **No publisher plumbing required** — it just grows as incremental DBs land.
- Total comes from `config.NATIONAL_TOTAL_ACTAS` (env `E14_NATIONAL_TOTAL`, default 121913).
- **Next:** deploy this so the live pilot already advertises the national rollout, then layer the
  hourly publisher (Phase 3) underneath it. (Optional later: have the publisher stamp an explicit
  "last published" time instead of relying on `processing_timestamp`.)

## 🆕 Local Claude labeling for the first seed pass (done locally)

- Decision: for the **first** seed batch, don't fully trust Gemma — label crops with a **local
  Claude Code (Haiku) session** instead. The live vote-based adjudication is unchanged (OpenRouter).
- New module `e14detector/labeling.py` + CLI:
  - `e14detector label-export --output-dir <dir> [--limit N] [--only-flagged] [--include-labeled] [--department X] [--shuffle --seed S]`
    → writes `review/label_queue.jsonl` (one crop per line: `field_key`, `path`, `label:null`) and
    `review/LABELING.md` (the rubric + protocol, reusing `_RUBRIC` so the CLEAN/DIRTY definition is
    identical to the pipeline). Default exports only **unlabeled** candidates.
  - A second local Claude session reads each `path`, writes `review/label_done.jsonl`
    (`{field_key|path, label}`).
  - `e14detector label-import --output-dir <dir> [--labels FILE]` applies them via
    `set_field_classification` (writes the vote_field row only, never the screen cache):
    **DIRTY → `SUSPICIOUS_OVERLAP`** (public seed), **CLEAN → `CLEAN`** (confirmed, overrides any
    prior Gemma flag). Matches by crop path first, then `field_key`.
- The existing gold set (`claude_labels.jsonl`, has `path`) is importable as-is.

## 🎯 Target architecture (the end goal, restated)

Three loops, all decoupled, platform publicly usable the entire time:
1. **Crop publisher** (continuous): the crop run incrementally ships new crops + rows to the
   platform; they appear live (per-request DB reopen + crops on Tigris). *Not built yet — Phase 2/3.*
2. **Seed sampler** (async, independent): take a sample **from the already-published pool** and run
   Haiku (`label-export`/`label-import`) or Gemma (`vlm-review --sample-rate`) on it, writing
   `vlm_classification` → those rows become public seeds. Both paths are incremental (only touch
   `vlm_classification IS NULL`), so "add 50 more next time" just works. **Guide + Haiku agent
   prompt: `docs/SEEDING.md`.** *Pieces built; orchestration is Phase 3.*
3. **Live poll** (already live): crowd flags → OpenRouter VLM adjudicates → publishes "strange".

Ranking goal: flagged seeds first, **and the most-voted actas floated to the top — silently**.

## 🆕 Silent most-voted ranking on /browse (done locally)

- The main `/browse` list now floats the **most-voted actas to the very top** (ordered by distinct
  voters desc), then flagged seeds, then region order. **No counts shown** — pure ordering.
- `_voted_doc_rows()` returns the top-voted actas (capped at `VOTED_FLOAT_CAP=300`, < SQLite's
  bound-param limit); the "rest" query excludes them via `NOT IN`. Pagination splices the floated
  prefix then the rest (offset adjusted by the floated count) — no dupes/skips across pages.
- `acta_popularity()` is now read on every /browse request (was page-1 only). Fine for launch;
  **future:** cache it if traffic warrants.

## ⚠️ Working-tree state to resolve first

- **Uncommitted confirm-tier code** in `config.py`, `storage.py`, `vlm_review.py`
  (`CONFIRM_MODEL=anthropic/claude-sonnet-4.6`, `run_seed_confirm()`, `flagged_candidate_fields()`,
  `set_field_classification()`). It is NOT wired into the CLI yet. **Decision pending:** keep + wire as
  the two-tier confirm step, or drop. (We chose two-tier seeding, so likely KEEP + add a CLI subcommand.)
- Partial national crops exist: `data/detector_national/` (~13.5k candidate crops done before we stopped
  for a reboot). Resumable — `process` skips finished actas. Wipe if we change crop ordering.

---

## Source data & sizing (measured)

- National PDFs: **121,913 actas, 23 GB** under `data/actas/<dept>/<muni>/<zone>/<...>.pdf`.
- Expected crops: **~1.58M candidate crops, ~9 GB, ~4.1M total files**.
- Crop run: **~18 h** on 12 cores (`--crop-only`, no CV). Disk free: ~900 GB. ✔ fits.
- 5% seed sample ≈ ~6k actas → Gemma cost ≈ a few dollars.

---

## Decisions locked in

- **Two-tier seeding**: cheap screen (Gemma) over the 5% sample, then **Claude (`claude-sonnet-4.6`)
  re-checks only the flagged ~1-3%** to kill false positives before they're shown publicly.
- **Crop hosting = Fly Tigris** (S3-compatible, provisioned from Fly; no AWS console).
- **Progressive rollout**: the webapp opens a **fresh read-only DB connection per request**, so adding
  rows to the results DB + crops to Tigris makes them appear live with **no redeploy**.
- **Incremental DB updates** (not full snapshots): each checkpoint ships only the new rows to Fly.
- **Region-ordered crop run**: process whole departments at a time so each hourly checkpoint publishes
  coherent, browsable chunks.

---

## Resume checklist

### Phase 0 — housekeeping
- [ ] Reboot done; confirm 12 cores at higher clock (`nproc`, `lscpu | grep MHz`).
- [ ] Decide on the confirm-tier code: **keep + add `vlm-confirm` CLI subcommand** (recommended) or revert.
- [ ] Commit whatever we keep so the tree is clean before the long run.

### Phase 1 — one-time scale plumbing (do while crops bake; safe — pilot keeps working)
- [ ] Provision Tigris: `fly storage create` (gives bucket + S3 creds as Fly secrets).
- [ ] Add a `raw_crop_path → CDN URL` mapping; webapp templates emit `<img src="https://<cdn>/…">`
      instead of `/crop` (keep `/crop` as fallback). One small change in `webapp.py` + `acta.html`/`browse.html`.
- [ ] Move the **national** `results.sqlite` to the Fly **volume** path (e.g. `/data/results.sqlite`),
      not baked into the image. Wire `cmd_serve` / asgi to read it from there.
- [ ] Confirm the per-request `conn()` reopen picks up a replaced/updated DB file.

### Phase 2 — national crop run (region-ordered, resumable)
- [ ] (Optional) wipe partial: `rm -rf data/detector_national`.
- [ ] Launch crop-only over all actas (region order), 12 workers, detached:
      `nohup .venv/bin/e14detector process --input-dir data/actas --output-dir data/detector_national --crop-only --workers 12 > logs/national_crop.log 2>&1 &`
- [ ] Monitor: `find data/detector_national/crops -name '*candidate*' | wc -l` (target ~1.58M).

### Phase 3 — the hourly publisher loop (the "progressive" engine)
- [ ] Script that, every ~hour:
  1. Seed-pass new sampled actas → Gemma `vlm_classification` (only NULL rows in the sample).
  2. Confirm-tier (Claude) over the newly-flagged crops → demote false positives.
  3. Upload those actas' candidate crops → Tigris.
  4. Ship the **new rows** to the Fly volume DB (incremental) → live within the hour.
- [ ] Could run via `/loop` (Claude Code) or a plain `cron`/`while` loop on the PC.

### Phase 4 — verify & harden
- [ ] Watch the site grow each hour from a small first batch.
- [ ] Spot-check seeds against the ~1% base rate (flag rate should stay near 1%, not 3%+).
- [ ] (Optional) Litestream backup of `community.sqlite` (the votes) to Tigris.

---

## Command / fact quick-reference

- App: `e14-poll` · live URL `https://e14-poll.fly.dev` · flyctl at `/home/jcrubioa/.fly/bin/flyctl`.
- Deploy: `/home/jcrubioa/.fly/bin/flyctl deploy --ha=false`.
- Pilot data dir: `data/detector` · national: `data/detector_national`.
- Seed pass (manual): `.venv/bin/e14detector vlm-review --provider openrouter --output-dir <dir> --all-candidates --concurrency 32` (Gemma at conc. 32 did the 520-crop pilot in ~64s, 0 fails).
- Gold set: `data/detector/review/claude_labels.jsonl` (eval fixture — score any model's precision/recall against it).
- Env knobs (`.env`, gitignored): `E14_SCREEN_MODEL`, `E14_OPENROUTER_MODEL`, `E14_CONFIRM_MODEL`,
  `E14_LLM_SAMPLE_RATE=0.05`, OpenRouter key, `E14_FORM_*` bot-check, `E14_VOTER_SALT`.
- Fly secrets set: `E14_OPENROUTER_API_KEY`, `E14_VOTER_SALT`, (inert) `E14_TURNSTILE_SECRET`.

## Open questions / risks
- Pushing 4.1M tiny files to Tigris is slow → publisher should upload only **candidate** crops, ideally
  batched; consider packing per-acta. (Don't `aws s3 sync` 4M loose files.)
- National `results.sqlite` size with crop-only (no CV) — measure early; keep on the volume, never in S3.
- Incremental DB apply must not corrupt the read-only-served DB (use WAL; single writer = the publisher).
