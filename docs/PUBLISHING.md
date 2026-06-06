# Publishing — getting crops + DB from a detector machine to the live deployment

> **Superseded by [`e14 sync`](SYNC.md).** Prefer `e14 sync run` / `status` / `verify` /
> `restore`, which wrap the verbs below with the count-model consistency rules baked in
> (lock-aware, frontier-only, shrink-guard, chain-stamp, verify-first). The commands here still
> work and explain the mechanics, but the unified tool is the one you should reach for.

This is **loop #1** of the rollout: a machine that runs the detector (producing crops and a
results DB) pushes that data to the live site. The site grows on its own as you publish more.

It is **incremental and resumable from any machine** — including a different machine than the
one that did earlier uploads — because the object store (Tigris) is the source of truth and a
small command (`publish-reconcile`) rebuilds local state from it.

`<OUTPUT_DIR>` below is the detector output dir (`data/detector_national` for the national run).

---

## TL;DR — sync a (possibly fresh) machine to the deployment

```bash
# 0. one-time, on a machine that didn't do the earlier uploads (or if unsure of local state):
e14detector publish-reconcile --output-dir <OUTPUT_DIR>     # ~minutes; lists the bucket

# 1. then just run the continuous publisher — uploads only NEW crops + grows the live frontier:
e14detector publish-loop --output-dir <OUTPUT_DIR>
```

That's it. Everything already in the bucket is skipped; only newly-finalized crops upload, and
the live DB snapshot grows to include each acta once all its crops are up. **Votes are never
touched** by publishing (they live in Aurora — see [ARCHITECTURE.md](ARCHITECTURE.md)).

---

## What the detector machine produces

Inside `<OUTPUT_DIR>`:

| Path | What |
|---|---|
| `results/results.sqlite` | the **working DB** (full ~2 GB: documents, vote_fields, CV features, paths) |
| `crops/*.png` | candidate vote-box crops the public page shows |
| `review/uploaded_crops.txt` | the **upload manifest** — one object key per uploaded crop |

Two things get published from here, on independent cadences: the **crops** (PNG → object store)
and a **slim DB snapshot** (→ object store + pointer the Fly app swaps in).

---

## The upload manifest (why sync is incremental)

`publish-crops` records every uploaded crop's key in `review/uploaded_crops.txt`. A key is
exactly the object-store key: `crops/<hmac>.png` (see `webapp.crop_key` / `crop_obj_name`) — an
**opaque HMAC of the crop path** (see [Opaque crop keys](#opaque-crop-keys-anonymization)), so
**the same crop has the same key on every machine** (portable) while leaking no acta identity.

On the next run, the uploader builds its plan as *(all candidate crops) − (keys in the
manifest)*, so it only sends what's new. The DB publisher reads the **same** manifest to decide
which actas are safe to publish (the "frontier", below).

> The manifest may contain duplicate lines (re-runs append); that's harmless — it's loaded as a
> set. `wc -l` over-counts; `sort -u | wc -l` is the true unique count.

### `publish-reconcile` — rebuild the manifest from the bucket

A different machine starts with an empty manifest and would re-upload everything. Run this once
to seed it from the store (the source of truth):

```bash
e14detector publish-reconcile --output-dir <OUTPUT_DIR>
# reconcile: bucket had 841909 crop object(s); manifest 0 -> 841909 key(s)
```

- Lists every `crops/` object in the bucket (`ListObjectsV2`, paginated) and writes their keys
  to the manifest, **unioned** with anything already there (a just-uploaded key is never lost).
- Atomic write (temp + replace): an interrupted run never leaves a half-written manifest.
- Read-only on the bucket — it lists keys, never downloads bodies. ~1–3 min per ~1M crops
  (sequential paginated LISTs; Tigris latency dominates).
- It also fixes the DB side automatically, since the frontier is computed from the same manifest.

Flags: `--bucket` (default `$BUCKET_NAME`), `--prefix` (default `crops/`).

---

## Opaque crop keys (anonymization)

The crop object key **is** the public CDN URL path (`<CDN>/crops/<key>`). If that key were the
readable filename (`E14_PRE_<dept>_<mun>_<zone>_<puesto>_<mesa>_..._candidate_field.png`), then
opening a swipe-feed crop in a new tab would reveal exactly which mesa it is — defeating the
anonymized review. So the key's file part is an **HMAC-SHA256 of the round-agnostic crop path**,
truncated to 24 hex chars + `.png` (`webapp.crop_obj_name`). A plain hash would be reversible
(the path namespace is small and fully structured → rainbow-tableable), so the HMAC **must** be
secret-keyed.

**`E14_CROP_KEY_SECRET` must be identical on the webapp AND every upload/publish machine.**
The uploader computes the key when it PUTs the object; the webapp computes the same key when it
builds the feed URL. If the secrets differ, the served URL won't match any uploaded object and
crops 404. It falls back to `E14_FORM_TOKEN_SECRET` → `E14_VOTER_SALT`, so a single-secret deploy
works without a new var — just make sure that fallback value is set the same in both places.

The whole pipeline is **forward-only** (path → key, never key → path): upload, the manifest, the
publish frontier (`_prune_to_uploaded`), reconcile, and `cropaudit` all *compute* the key from a
known local/DB path, so the one-way HMAC needs no reverse index. The feed serves the opaque CDN
URL directly (no `/c/{cid}` redirect hop); `/c/{cid}` remains only as the no-CDN/local fallback.

### Migrating an existing bucket (one-time re-key)

A bucket published before this change holds readable keys and still leaks. Re-key it in place with
`scripts/rekey_crops.py` (server-side `CopyObject` — no egress, ~$10 of object ops for ~1.6M
crops). Run with the **same `E14_CROP_KEY_SECRET`** the webapp uses.

```bash
# 0. set E14_CROP_KEY_SECRET identically on the webapp (fly secret) AND publish machines
python -m scripts.rekey_crops --dry-run            # report how many keys would move
python -m scripts.rekey_crops --workers 32         # copy readable -> opaque (both coexist)
e14detector publish-reconcile --output-dir <OUTPUT_DIR>   # rebuild manifest from the bucket
# verify the app serves opaque /crops/<hmac>.png URLs and a new-tab open shows no mesa id, then:
python -m scripts.rekey_crops --delete-old         # remove the old readable (leaking) keys
e14detector publish-reconcile --output-dir <OUTPUT_DIR>   # clean opaque-only manifest
```

Both phases are idempotent and skip already-opaque keys; `--delete-old` only deletes an original
after confirming its opaque copy exists. Deploy the code to the webapp **and** publish machines
together (shared `crop_key`) so new uploads also land at opaque keys. R1 is frozen — apply the
same re-key to the R1 archive bucket as a follow-up, or keep that app offline.

---

## Publishing crops

```bash
e14detector publish-crops --output-dir <OUTPUT_DIR>
# uploaded=1234 skipped=841909 failed=0
```

- Incremental: uploads only crops not in the manifest; appends each success to the manifest.
- `--workers N` (default 16) upload concurrency; `--limit N` caps a run; `--dry-run` counts only.
- `--department 16` restricts the plan to one department (divide-and-conquer across machines).
  Convenience: `bash scripts/publish_dept_crops.sh 16`.
- Idempotent: re-uploading an existing key just overwrites identical bytes — safe, only wasteful.

---

## Publishing the DB snapshot (the "frontier")

```bash
e14detector publish-db --output-dir <OUTPUT_DIR> --only-uploaded
# published db: db/results-<sha>.sqlite.gz (46.7 MB, sha=<sha>)
```

What it does:
1. Builds a **slim serving snapshot** (`build_serving_db`) — candidate registry + geo +
   `documents.n_candidates` only; drops CV/verdict/path columns. ~2 GB → ~730 MB (gz ~46 MB).
2. With `--only-uploaded`, prunes it to the **frontier**: only actas whose candidate crops are
   *all* in the upload manifest. So the live site never references a crop that isn't uploaded yet.
3. Uploads the gzip under a content-addressed key and flips `db/latest.json` (the pointer) last.

The frontier grows **monotonically** as more crops upload — it never drops an acta already live.

### Shrink guard

`publish-db` refuses to replace the live DB with one **< 50 % its raw size** (protects against a
misconfigured `--output-dir` pointing at a stub DB nuking the national DB). Override only when a
shrink is genuinely intended: `--allow-shrink`.

---

## `publish-loop` — the hands-off publisher

```bash
e14detector publish-loop --output-dir <OUTPUT_DIR>
# [publish-loop] +320 crops (fail 0) · frontier 64761 actas (sha 6ccf62b1) · 7s
```

Runs alongside the detector run: each tick uploads new crops (cheap delta), and every
`--db-interval` republishes the frontier DB. The live page grows on its own.

- `--interval` (default 60 s) between crop ticks · `--db-interval` (default 300 s) between DB
  publishes · `--upload-limit` (default 12000) caps crops per tick so the frontier publishes
  often · `--workers` (default 32) · `--once` runs a single cycle and exits.
- One bad cycle logs and continues — the loop never dies on a transient error.

> **One publisher at a time.** Run the loop on a single machine. Two machines publishing the
> same bucket diverge their manifests and double-upload (harmless but wasteful). When handing
> off to a new machine, `publish-reconcile` there and stop the loop on the old one.

---

## How the live site picks it up (reader side)

The Fly app (`e14-poll`) polls the pointer and atomic-swaps — no redeploy:

- `E14_DB_SYNC=1`, `E14_DB_SYNC_INTERVAL=60` (seconds): every interval it fetches
  `db/latest.json` from the CDN; if the sha changed, it downloads the gz, decompresses, and
  `os.replace()`s the served file (atomic — never serves a half file).
- After a swap it **prewarms** the OS page cache so the next feed/browse is a memory hit.
- Crops are served straight from the CDN (`E14_CDN_BASE_URL`) as `/c/{cid}`.

So end to end: `publish-loop` on the detector box → bucket + pointer → Fly reader swaps in the
new snapshot within ~a minute → visitors see the new actas. Votes already cast stay valid
because they key on a **stable field_key** (acta identity), not on DB row ids.

---

## Credentials the publisher needs

Publishing talks to **Tigris** (the object store), which uses the standard S3 env names — these
are the **Tigris** keys, not real AWS (see [the credential split](ARCHITECTURE.md#credentials)):

```
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL_S3, BUCKET_NAME
```

Put them in the detector machine's `.env` (git-ignored). `E14_CDN_BASE_URL` is the public read
URL the served page uses.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `publish-crops` re-uploads everything on a new machine | empty manifest — run `publish-reconcile` first |
| `published db: nothing in the uploaded frontier yet` | no acta has *all* its crops uploaded yet — upload more crops first |
| `published db: GUARDED — refused to shrink` | wrong `--output-dir`, or an intended shrink — re-check the path, then `--allow-shrink` |
| `no bucket: set BUCKET_NAME` | missing Tigris env — populate `.env` |
| Live site not updating | check the Fly app has `E14_DB_SYNC=1`; confirm the pointer sha changed |
