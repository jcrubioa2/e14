# Seeding guide — labeling samples into public "strange" seeds

This is **loop #2** of the rollout: independently of the crop publisher and the live vote
poll, you take a **sample of crops from the already-published pool** and classify them
CLEAN/DIRTY. A `DIRTY` verdict becomes a public **seed** (the acta shows up as *"señalada
para revisar"*); `CLEAN` is recorded as confirmed-clean and never shows.

There are two interchangeable ways to classify a batch:

- **Path A — local Claude (Haiku)**: a separate local Claude Code session reads the crop
  images and labels them. Highest precision; free; good for the first seed batches.
- **Path B — OpenRouter (Gemma)**: one CLI command classifies a batch via the API. Hands-off;
  costs a few cents per thousand crops.

Both write the same column (`vote_fields.vlm_classification`), so you can mix them freely.

> The **live vote-based adjudication is unaffected** by either path — it always runs on
> OpenRouter. Seeding only sets the *initial* "strange" basis.

---

## Why every run is incremental

Both paths only ever touch crops with **no verdict yet** (`vlm_classification IS NULL`):

- `label-export` exports only unlabeled candidate crops by default.
- `vlm-review` selects `WHERE vlm_classification IS NULL`.

So once a crop is classified (and, for Path A, imported), it drops out of the pool. The next
run with `--limit 50` automatically draws **50 _different_** crops. To grow the seed set you
just run the loop again with the batch size you want — no bookkeeping, no overlap.

`<OUTPUT_DIR>` below is the detector output dir for the run you're seeding —
`data/detector` for the pilot, `data/detector_national` for the national run.

---

## Path A — local Claude (Haiku), incremental loop

### 1. Export a batch (operator, in this repo)

```bash
e14detector label-export --output-dir <OUTPUT_DIR> --limit 50 --shuffle
```

This writes:
- `<OUTPUT_DIR>/review/label_queue.jsonl` — 50 unlabeled crops (`field_key`, `path`, `label:null`)
- `<OUTPUT_DIR>/review/LABELING.md` — the rubric + protocol

Useful variants:
- `--only-flagged` — re-check crops Gemma already flagged (audit its false positives).
- `--department "ANTIOQUIA"` — restrict the batch to one department.
- `--shuffle --seed 7` — random draw (change the seed for a different ordering).

### 2. Label the batch (a fresh Claude Code session, Haiku)

Open a **separate** Claude Code session in this repo, set the model to Haiku, and paste the
prompt in [the next section](#prompt-for-a-fresh-haiku-agent). It reads each crop and writes
`<OUTPUT_DIR>/review/label_done.jsonl`.

### 3. Import the labels (operator, in this repo)

```bash
e14detector label-import --output-dir <OUTPUT_DIR>
# -> labels applied: 1 DIRTY (seeded), 49 CLEAN (confirmed) · skipped 0 · unmatched 0
```

`DIRTY → SUSPICIOUS_OVERLAP` (public seed); `CLEAN → CLEAN` (also overrides any prior Gemma
flag on that crop). The platform reflects it on the next page load — no redeploy.

### 4. Next time: "add 50 more"

Just repeat steps 1–3. Because the 50 you imported now have a verdict, step 1 draws 50 brand
new ones.

> **Cadence rule:** import (step 3) **before** exporting the next batch, and have the agent
> write a **fresh** `label_done.jsonl` per batch (overwrite, not append). That keeps each
> batch clean and the pool advancing.

---

### Prompt for a fresh Haiku agent

Start a new Claude Code session in this repo, run `/model` → Haiku, then paste:

```
You are labeling a batch of E-14 vote-count crops. Work entirely from these files in this repo:

- Queue (input):  <OUTPUT_DIR>/review/label_queue.jsonl   (JSONL, one crop per line)
- Output:         <OUTPUT_DIR>/review/label_done.jsonl

Each crop is one candidate's hand-written vote box (≤3 digits). The poll judge fills any
unused digit position with a plain placeholder mark — an asterisk (*), a dash (-) or a dot
(.). A box made only of these marks just means a low/zero count and is completely normal.

Label each crop with ONE word:
- CLEAN = ordinary separate digits and/or plain placeholder marks, with no overlap.
- DIRTY = a real digit written ON TOP OF a placeholder mark (clearly overlapping/merged with
  it), or other clear tampering.
When unsure, choose CLEAN — real tampering is ~1% of crops, and a false seed is costly.

Steps:
1. Read every line of <OUTPUT_DIR>/review/label_queue.jsonl. Each line has a "path" (the
   image) and a "field_key" (its id).
2. Open each crop with the Read tool and look at it.
3. Write <OUTPUT_DIR>/review/label_done.jsonl FRESH (overwrite any existing file). Append one
   JSON object per crop:
   {"field_key": "<copy from the queue line>", "label": "CLEAN"}
4. Label every crop exactly once. Output only CLEAN or DIRTY — no other text in the labels.

When finished, report how many CLEAN vs DIRTY you wrote. Do not run any e14detector commands;
the operator will import your labels.
```

Replace `<OUTPUT_DIR>` with the real path before pasting (e.g. `data/detector_national`).

---

## Path B — OpenRouter (Gemma), incremental loop

One command classifies the next N unlabeled candidate crops via Gemma (`E14_SCREEN_MODEL`,
the neutral SCREEN prompt). Needs `E14_OPENROUTER_API_KEY` in `.env`.

```bash
# add 50 more (different) crops to the seed set:
e14detector vlm-review --provider openrouter --output-dir <OUTPUT_DIR> \
    --all-candidates --limit 50 --concurrency 32
```

- `--all-candidates` reviews every candidate (CV isn't run in the crop-only national pass), not
  just CV-flagged rows.
- `--limit 50` caps the batch; re-running picks the next 50 unlabeled crops (incremental).
- Omit `--limit` to classify the entire remaining pool in one go.

Alternative — classify a **deterministic % of whole actas** instead of a flat crop count:

```bash
e14detector vlm-review --provider openrouter --output-dir <OUTPUT_DIR> --sample-rate 0.05
```

`--sample-rate` always picks the *same* actas across runs (hash of `document_id`), and skips
already-classified crops, so it's safe to re-run as more crops land.

---

## Verifying a batch landed

- Public list: open `/browse` — newly DIRTY actas show *"señalada para revisar"* and sort up.
- Spot-check the flag rate stays near the ~1% base rate (not 3%+). If Gemma over-flags, prefer
  Path A (local Claude) for that batch, or re-check Gemma's positives with
  `label-export --only-flagged`.

See `plans/pending/national-rollout.md` for how this loop fits the full rollout.
