# Served DB — what to do and what NOT to do (read before touching publishing)

> **If you are an AI agent or a new contributor about to run anything that publishes the
> database, change `publish_db`, change `merge_results_db`, or "fix" the live front — read
> this first.** This file exists because we already broke the live site twice the same way.

The served database is the single source of truth the public site reads. Every acta and vote in
it is **irreplaceable** (manually crowd-verified election data). There is no "re-run to
regenerate it" — a bad publish destroys real work. Treat it like production data, because it is.

---

## The one-paragraph mental model

A detector machine builds a **fat** national `results.sqlite` (~30 columns, ~2 GB). Publishing
runs `build_serving_db` to produce a **slim** serving snapshot (~9 vote_fields columns, registry
+ geo, adds `n_candidates`, ~760 MB), gzips it, and uploads it **content-addressed** to the
Tigris bucket `e14-crops` under `db/results-<hash>.sqlite.gz`. It then flips the pointer
`db/latest.json` to that object. The Fly app (`e14-poll`) polls the pointer every ~60s and
atomic-swaps the served DB. **Slim is intentional** — it is what keeps the Fly app's memory low.
Never publish the fat working DB.

---

## ✅ DO

- **Use the current `e14detector` code on `main`** to publish. `main` has all the guards below.
- **Publish with `publish-loop` / `publish_db`** as documented in
  [PUBLISHING.md](PUBLISHING.md). It only uploads new crops and grows the frontier; votes are
  never touched (they live in Aurora).
- **Keep the served DB slim** (`build_serving_db`). If a snapshot is suspiciously large, stop.
- **Let `merge_results_db` / `pull-db` reconcile machines.** As of the fixed merge it copies the
  **intersection** of columns present in both DBs, so a fat local DB and a slim published
  snapshot merge cleanly in either direction.
- **Lock the DB once it reaches 100%** from the admin board (the 🔒 toggle). When locked,
  `publish_db` refuses to overwrite the served DB.
- **To intentionally publish past a lock** (rare, deliberate), unlock from the admin board first,
  or pass `allow_locked=True` / `E14_DB_ALLOW_LOCKED=1` — and know exactly why.
- **Keep credentials straight:** `AWS_*` = Tigris (bucket/publishing). `E14_VOTE_AWS_*` = real
  AWS (SQS/Aurora). They are different services with different keys. **Never cross them.**

## ⛔ DO NOT

- **Do NOT publish the fat working DB.** Always go through `build_serving_db` (publish_db does).
- **Do NOT publish from old / forked code that predates the lock and the column-intersection
  merge.** The lock is *cooperative* — only current-code publishers honor it. An old publish
  loop or a stale branch checkout will happily clobber a locked, complete DB.
- **Do NOT unlock the DB to "make a publish go through"** unless you genuinely intend to replace
  the served DB and have confirmed the new one is complete (`n_docs` ≥ live).
- **Do NOT bypass the `n_docs` guard with `allow_shrink=True`** unless you have verified the acta
  count is correct and the smaller bytes are only a slim-down. The guard refuses a publish whose
  acta count is < 50% of the live DB — that is almost always a mistake (wrong `--output-dir`,
  stub DB, partial run), not something to override.
- **Do NOT enable `E14_DB_MERGE_BEFORE_PUBLISH`** as a default. It is opt-in (default OFF) on
  purpose. A silent auto-merge-before-publish is one of the things that shipped a partial DB.
- **Do NOT "fix the live front shows X%" by republishing whatever local DB you happen to have.**
  See the recovery procedure below — the complete DB is usually already an immutable bucket
  object; republishing a local partial is exactly how the regression happens.

---

## Why this file exists — the two regressions

1. **Byte-shrink false-trip.** The original publish guard compared raw bytes. A legitimate
   *slim-down* (fat → serving schema) halves the bytes while keeping every acta, so the guard
   refused a perfectly good publish. **Fix:** the guard keys on `n_docs` (acta count), not bytes.
   A slim-but-complete snapshot publishes; a real drop in actas is still refused.

2. **Partial publish clobbered the complete DB → front dropped to 78.1%.** A lock-unaware
   publisher running old code, plus a broken `merge_results_db` (it built the SELECT list from
   the *local* fat columns and ran it against the *slim* remote → `no such column: page_number`,
   which the auto-merge then swallowed), let a partial ~95k-acta DB overwrite the complete
   122,007-acta DB. **Fixes:** (a) `merge_results_db` now merges on the column intersection;
   (b) auto-merge is opt-in and no longer swallows errors; (c) the **publish lock** freezes the
   served DB once complete; (d) the `n_docs` count-guard. The broken branch
   (`feature/multi-machine-crop-sync`) has since been **fixed and merged into `main`** — current
   code is safe; *old checkouts are not*.

---

## The guards in `e14detector/dbsync.py` (do not weaken these)

- **Publish lock** — `db/lock.json` in the bucket. `read_db_lock` / `set_db_lock`; toggled from
  the admin board (`POST /admin/db-lock?key=…&locked=on|off`). `publish_db` checks it **first**
  (before any pull/merge or the slim build) and refuses when locked unless
  `allow_locked` / `E14_DB_ALLOW_LOCKED=1`.
- **`n_docs` count-guard** — `publish_db` refuses to flip the pointer to a DB with < 50% of the
  live DB's acta count, unless `allow_shrink=True`. The count is invariant to schema slimming, so
  it does not false-trip on a legitimate slim-down. (Legacy pointers without `n_docs` fall back to
  the byte heuristic exactly once, then record `n_docs`.)
- **Column-intersection merge** — `merge_results_db` copies only columns present in **both**
  tables (explicit column lists, never `SELECT *`), for `documents` and `vote_fields`. This is
  what lets fat-local and slim-remote DBs merge without errors or positional misalignment.
- **Opt-in auto-merge** — `E14_DB_MERGE_BEFORE_PUBLISH` defaults OFF. When set
  (`1`/`true`/`yes`), a merge failure **aborts** the publish instead of being swallowed.

If you change any of these, update this doc and the tests in `tests/test_dbsync.py`
(`test_publish_db_refuses_when_locked`, `test_publish_db_refuses_when_acta_count_drops`,
`test_publish_db_allows_smaller_bytes_when_acta_count_holds`,
`test_merge_results_db_union_without_overwriting_local`).

---

## Recovery: "the front shows less than 100%"

Do **not** reflexively republish a local DB. Instead:

1. **Find the complete snapshot.** It is almost always still in the bucket as an immutable
   content-addressed object (`db/results-<hash>.sqlite.gz`) — partial publishes only move the
   *pointer*, they don't delete prior objects. Check object `n_docs` / size against the known
   complete count (122,007 actas at the time of writing).
2. **Republish the complete object through current `publish_db`** (it re-slims, adds
   `n_candidates`, writes `n_docs`). Use `allow_shrink=True` only if you are intentionally moving
   the pointer back to the larger-acta DB and the byte-guard would otherwise trip.
3. **Lock it** from the admin board once the front reads "Todas las actas están disponibles".
4. **Find and stop whatever republished the partial** (old code / stale branch / rogue loop)
   before relying on the lock — the lock only stops *current-code* publishers.

See [PUBLISHING.md](PUBLISHING.md) for the normal loop and [MULTI_MACHINE.md](MULTI_MACHINE.md)
for fleet coordination.
