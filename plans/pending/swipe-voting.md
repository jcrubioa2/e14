# Plan: Tinder-style anonymous crop voting (crowd-only, VLM tabled)

> Checklist for a fresh agent. Each `[ ]` is a discrete, verifiable step.

## Context

The VLM poll-adjudication path hit a precision/recall wall (see
`plans/pending/voted-crop-adjudication.md` and memory `e14-voted-crop-model-sweep`): no single
model catches the hard "131-over-placeholder" case without rubber-stamping. **Decision: table the
LLM approach entirely** and make the *crowd* the verdict.

New product: a viral, mobile-first **swipe feed** where anyone votes **"se ve bien" / "se ve
extraño"** on **individual, anonymized candidate crops shown in random order**. All tallies are
**fully public**. Voting moves *exclusively* to the feed; the acta lookup becomes read-only and
just displays the public counts. A crop-level **billboard** surfaces the hottest crops.

Why this shape: it's high-value / low-effort — it reuses the existing vote storage, dedup
(`voter_token`), and bot-check, and deletes the fragile VLM adjudication wiring instead of adding to it.

### Core rules (non-negotiable, from the user)
1. Voting is **randomized + anonymized** — a voter must not know which acta/location a crop is from.
2. Results are **entirely public** (counts visible everywhere: feed, acta view, billboard).
3. **All** voting goes through the feed. No voting in the acta view.
4. Both options are tracked (good **and** strange). **Best-effort dedup** only; no vote-changing.

---

## Phase 0 — Remove the VLM / poll-adjudication path

- [ ] In `e14detector/webapp.py`, stop triggering VLM on votes: remove the
      `try_claim_adjudication()` → `schedule_adjudication()` calls from the flag flow (around
      `webapp.py:1193`) and the appeal equivalent. Delete/neuter `adjudicate()`,
      `adjudicate_appeal()`, `schedule_adjudication()`, `_review_crop_consensus()`
      (`webapp.py:556–625`).
- [ ] Drop **Gemma seed** flagging from the public UI: the feed is random over *all* candidate
      crops, nothing is pre-flagged. Remove `chip seed` / "para revisar" rendering from
      `acta.html` and `browse.html`. (Leave the offline detector/`cropper.py` that *produces*
      crops untouched — that is not the VLM.)
- [ ] Mark the now-unused poll config as deprecated in `config.py` (`POLL_THRESHOLD`,
      `POLL_RESCALE_STEP`, `POLL_CONSENSUS_K/_TEMP`, `APPEAL_THRESHOLD`, `APPEAL_RESCALE_STEP`).
      Don't delete `voter_token`, `FORM_TOKEN_SECRET`, `FORM_MIN/MAX_SECONDS`, `RATE_*`,
      `HIGH_VOTE_THRESHOLD`, `HOTLIST_SIZE`.
- [ ] `vlm_review.py` adjudication entrypoints are no longer called from the web path — leave the
      module for seed/offline use but confirm nothing in `webapp.py` imports its adjudication fns.

## Phase 1 — Anonymous crop identity (the linchpin)

Today `/crop?path=` and `field_key` (`{document_id}:{page}:{row}:{section}`) both leak the acta.
We need an **opaque, reversible-on-server** id.

- [ ] Add a crop-id map built once at startup from the results DB (all `vote_fields` rows with
      `row_type='candidate'` and `raw_crop_path IS NOT NULL`). For each: compute
      `cid = hmac_sha1(FORM_TOKEN_SECRET, field_key)[:12]` and store **both directions** in
      memory: `cid → (field_key, crop_rel_path, document_id)` and `field_key → cid`. Put the
      builder near `resolve_crop_path` (`webapp.py:281`) or in `community.py`.
- [ ] New route `GET /c/{cid}` → resolves `cid` to the crop file and serves it via `FileResponse`
      (reuse the `output_dir` containment check from `resolve_crop_path`). **Never** echo the path
      or document_id. This replaces public use of `/crop?path=`.
- [ ] Audit that no public template/JS/JSON ever emits `field_key`, `document_id`, `raw_crop_path`,
      or geographic fields for a feed crop. The admin view (`admin.html`) may keep real ids.

## Phase 2 — Vote backend (reuse flags + appeals)

- [ ] Repurpose the two tallies on **any** crop: `strange → community.record_flag`,
      `good → community.record_appeal`. Remove the "appeal only allowed if currently strange"
      eligibility gate (`webapp.py:1202+`).
- [ ] New endpoint `POST /api/vote` taking `{ cid, value: "good"|"strange", form_token, website }`.
      Server: `bot_check()` (honeypot + signed form token, `webapp.py:1252`) → rate-limit
      (`RATE_*`) → map `cid → field_key` → `record_flag`/`record_appeal` (both `INSERT OR IGNORE`
      on `UNIQUE(field_key, voter_token)` = best-effort dedup via daily IP hash). Idempotent: a
      duplicate returns 200 with the current public tallies, never an error.
- [ ] Retire `POST /api/flag` and `POST /api/appeal` (or alias them to `/api/vote`) so the acta
      page can no longer vote.
- [ ] Add public count accessors in `community.py`: `good_count(field_key)`,
      `strange_count(field_key)`, and a batch variant for lists. Counts are **public** now (today
      they're admin-only).

## Phase 3 — The swipe feed UI (the headline)

- [ ] New route `GET /votar` (link it from `/` and `/browse`) rendering `templates/swipe.html`:
      a mobile-first deck of anonymized crop cards in random order.
- [ ] New endpoint `GET /api/feed?n=...` → returns a random batch of `{ cid, img_url }`
      (`ORDER BY RANDOM()` over candidate crops, or shuffle in Python). `img_url = /c/{cid}`.
      Best-effort "don't repeat": client tracks swiped `cid`s in `localStorage`; cross-session
      dedup is the vote tables silently ignoring repeats.
- [ ] Swipe interaction in inline JS (match existing inline-JS pattern; no static asset dir):
      swipe/drag or two big buttons → **bien / extraño** → `POST /api/vote` → advance card →
      prefetch next batch when the deck runs low. Show the updated public good/strange tally
      after each vote.
- [ ] **Scroll-down context:** each card can expand to show *all sibling crops of the same acta*,
      shuffled and still anonymized. New endpoint `GET /api/acta-crops?cid=...` → resolves the
      card's `document_id`, returns the other candidate crops as `{ cid, img_url, good, strange }`
      in random order. **No acta id, no location, no candidate names** in the response.

## Phase 4 — Public billboard of hot crops

- [ ] Crop-level billboard (extend or replace the acta-level `_build_hotlist`, `webapp.py:891`).
      Rank candidate crops by community attention (e.g. `strange_count`, or
      `strange_count + good_count` with strange weighted). Each entry = `{ cid, img_url, good,
      strange }`, anonymized, linking into `/votar` (not into the acta).
- [ ] Render the billboard on a dedicated section/page (top of `/votar` and/or `/browse`),
      publicly showing the counts. Cap at `HOTLIST_SIZE`.

## Phase 5 — Acta view becomes read-only + public tallies

- [ ] In `templates/acta.html`: remove **both** vote buttons (`.flag`, `.normal`,
      `acta.html:134–137`) and their inline JS handlers (`acta.html:147–189`).
- [ ] For each crop show the **public good/strange counts** (via the new batch accessor).
      Keep the `chip alta` "muy reportada" badge driven by `HIGH_VOTE_THRESHOLD` if desired.
- [ ] Optional: a "vótalo en el feed" link per crop deep-linking into `/votar` (only if it can be
      done without de-anonymizing — i.e. link to the feed generally, not to that crop's acta).
- [ ] Keep normal lookup/`browse` working (cascading dept→municipio→zona→puesto,
      `/api/places`). Surface public crop counts there too.

## Phase 6 — Verification

- [ ] Unit/integration (extend `tests/test_community.py`): `POST /api/vote good/strange` updates
      the right tally; duplicate vote (same `voter_token`) is a no-op; bot-check rejects missing/
      forged `form_token` and honeypot-filled submits; rate limit returns 429.
- [ ] Anonymization test: assert `/api/feed`, `/api/acta-crops`, `/c/{cid}` responses contain **no**
      `document_id` / `field_key` / path / location substring. `cid` round-trips to the right
      `field_key` server-side.
- [ ] Manual: run locally (`uvicorn e14detector.asgi:app`), open `/votar` on a phone viewport,
      swipe a few crops, confirm tallies increment, scroll a card to see anonymized acta siblings,
      check the billboard updates, and confirm `/acta/{id}` shows counts with **no** vote buttons.
- [ ] `pytest -p no:cacheprovider` (full collection hangs on the WSL mount without this flag —
      see memory `e14-voted-crop-model-sweep`).
- [ ] No secrets touched; repo is PUBLIC/GPLv3. Deploy is a code change → single-machine Fly blip
      on `fly deploy` is expected and accepted (per prior decision).

---

## Deferred (future, not v1) — optional "type the number you read"

- [ ] Optional numeric input on each card: voter types the 1–3 digit count they read.
- [ ] Store submissions per `field_key` (new small table or a column) for a future crowd-consensus
      reading. Keep it optional/skippable so it never slows the swipe.
- [ ] Later: compute and publicly display the consensus read alongside good/strange tallies.

## Critical files
- `e14detector/webapp.py` — routes (`/votar`, `/api/feed`, `/api/vote`, `/api/acta-crops`,
  `/c/{cid}`), remove adjudication (`556–625`, `1153–1247`), billboard (`891`), acta (`1074`).
- `e14detector/community.py` — `record_flag`/`record_appeal`, drop adjudication claims, new public
  count accessors, cid map.
- `e14detector/config.py` — deprecate `POLL_*`/`APPEAL_*`; keep bot/rate/hotlist keys.
- `e14detector/storage.py` — random candidate-crop query for the feed.
- `e14detector/templates/swipe.html` (new), `acta.html` (strip voting, add tallies),
  `browse.html` (crop billboard + public counts), `admin.html` (unchanged, may keep real ids).
- `tests/test_community.py` — vote/dedup/bot-check/anonymization tests.
