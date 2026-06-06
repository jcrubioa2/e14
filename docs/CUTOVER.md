# Round-1 → Round-2 cutover runbook

The deliberate, one-time flip that turns the live app into the **runoff (R2)** primary and freezes
the **first round (R1)** as a permanent read-only archive. Phase 2 / WS-E. Everything here is a
**single documented step** via `scripts/deploy.sh`; nothing runs automatically.

> **Golden rule:** R1 is the permanent public record. It stays byte-identical until this cutover,
> and after it, the R1 archive must keep serving exactly what it served. When in doubt, stop.

## Round model (what is physically separate)

| Resource | R1 (frozen archive) | R2 (runoff primary) |
|---|---|---|
| Fly app | `e14-r1-archive` (`fly.r1-archive.toml`, scale-to-0) | `e14-poll` (`fly.toml`) |
| `E14_ELECTION_ROUND` | `r1` | `r2` (set as a secret at cutover) |
| Pointer / lock / snapshots | `db/latest.json`, `db/lock.json`, `db/results-*.gz` | `db/r2/…` |
| Crops | `crops/<hmac>.png` | `crops/r2/<hmac>.png` |
| Universe snapshot (local) | `data/universe_snapshot.json` | `data/r2/universe_snapshot.json` |
| Vote backend (Aurora + SQS) | retired after bake (verdicts baked into the archive snapshot) | fresh `E14VoteStackR2` |
| Tigris bucket | one bucket, round-prefixed (NOT a second bucket) | same bucket |

The bucket keys for R1 never move (the `r1` round maps to the legacy un-prefixed prefix), so standing
up R2 moves zero R1 data. The crop **file part is an opaque HMAC** of the path (`webapp.crop_obj_name`),
so the public CDN URL leaks no acta identity — see the [anonymization re-key](#anonymization-re-key-one-time-independent-of-r1r2)
section and [PUBLISHING.md](PUBLISHING.md#opaque-crop-keys-anonymization). Both prefixes (`crops/`
and `crops/r2/`) are keyed with the **same `E14_CROP_KEY_SECRET`**; it must be set, identical, on
`e14-poll`, the publish machine, and `e14-r1-archive` (which reads the same bucket).

## Pre-cutover (do days ahead, no live impact)

1. **Local verification gate (must pass first).** With `E14_ELECTION_ROUND=r2` locally, run the
   round-scoped pipeline on real/simulacro R2 actas and eyeball the debug crops. R2 layout coords
   live in `e14detector/layout.py` `LAYOUT["r2"]` and ship as a STUB (`ready=False`) that *refuses*
   to crop until calibrated — fill the real cell edges from a blank/simulacro acta and set
   `ready=True`. No deploy until crops look right and the chain verifies.
2. **Provision the R2 vote backend:** `cd infra && cdk deploy E14VoteStackR2`. Note the outputs
   (`AuroraClusterArn`, `AuroraSecretArn`, `SqsQueueUrl`) — these become the R2 secrets. R1's
   `E14VoteStack` is untouched. Aurora Serverless v2 scale-to-zero keeps the idle R2 cluster cheap.
3. **Apply the schema to R2 Aurora:** `infra/apply_schema.py` against the R2 cluster (same
   `infra/schema.sql`, unchanged — no `election_round` column; the cluster is single-round).
4. **Mint the R2 Fly user key** out-of-band (see `infra/README.md`) → `E14_VOTE_AWS_*` for `e14-poll`.

## Cutover (the deliberate flip)

5. **Freeze + lock R1.** Confirm the R1 pointer is at 100% and locked (`db/lock.json`), so no late
   publisher can move it: `e14 sync run --once` then set the lock via the admin board / `set_db_lock`.
6. **Bake R1 verdicts into the archive snapshot.** Snapshot R1's current Aurora verdicts
   (flags/appeals/field_state aggregates) into the served R1 SQLite so the archive app needs **zero**
   live vote backend. (R1 community voting is closed at cutover.)
7. **Stand up the R1 archive app** (first time creates app + volume):
   ```bash
   fly apps create e14-r1-archive
   fly volumes create data -a e14-r1-archive -r dfw -n 1 --size 3
   fly secrets set E14_CROP_KEY_SECRET=<value> -a e14-r1-archive   # SAME value as e14-poll
   ./scripts/deploy.sh r1-archive
   ```
   `E14_CROP_KEY_SECRET` is **mandatory** here: the archive reads the same `e14-crops` bucket, whose
   keys are opaque HMACs — without the matching secret every crop 404s. Verify it serves the frozen R1
   at `https://e14-r1-archive.fly.dev` (then attach the `primera-vuelta.<domain>` cert/DNS if desired,
   and set `E14_SITE_URL` to it).
8. **Flip `e14-poll` to R2** — one command (prompts for confirmation):
   ```bash
   R2_AURORA_CLUSTER_ARN=… R2_AURORA_SECRET_ARN=… R2_SQS_QUEUE_URL=… \
   R1_ARCHIVE_URL=https://e14-r1-archive.fly.dev \
   ./scripts/deploy.sh cutover-r2
   ```
   This sets `E14_ELECTION_ROUND=r2`, the R2 Aurora/SQS secrets, and `E14_R1_ARCHIVE_URL` on
   `e14-poll` (which triggers a rolling release). The page becomes R2 outright and grows a persistent
   **"Ver resultados de la primera vuelta →"** button to the archive (rendered only when serving r2).
9. **Verify both:** `./scripts/deploy.sh status`; load the R2 page (few/zero actas at first is fine,
   it grows live) and confirm the R1 button works; load the archive and confirm R1 is intact.

## Post-cutover

10. **Retire the R1 vote backend** once the archive is confirmed self-contained: pause/retire
    `E14VoteStack`'s Aurora + SQS (no ongoing cost, no shared surface). R1 has no live deps now.
11. Keep one **off-Tigris DR copy** of the R1 snapshot: `e14 sync backup --round r1 --dest <dir>`.

## Rollback

- **Bad R2 publish:** the lock + immutable content-addressed snapshots + the reader-side shrink/lock
  guard already protect serving. Roll the R2 pointer back to a known-good snapshot (per round).
- **Revert the whole flip:** point the custom domain back at the R1 archive (or unset
  `E14_ELECTION_ROUND` on `e14-poll` to return it to r1). The R1 archive is never mutated by R2, so
  the first-round record is always intact to fall back to.

## Anonymization re-key (one-time, independent of R1→R2)

A **separate** live cutover from the R1→R2 flip: it makes the crop object key (== the public CDN
URL path) an opaque HMAC so opening a swipe-feed crop in a new tab no longer reveals the mesa.
Mechanics + commands live in [PUBLISHING.md](PUBLISHING.md#migrating-an-existing-bucket-one-time-re-key);
this is the production go/no-go ordering. Reversible until the final delete.

**Prerequisite — the shared secret (freeze it; never change after migration, or all keys orphan):**
- `fly secrets set E14_CROP_KEY_SECRET=<value> -a e14-poll`
- publish machine: `E14_CROP_KEY_SECRET=<value>` in `.env` (append, then `git secret hide` to persist
  into `.env.secret`). Same value as `e14-poll`.
- Do **not** rely on the `FORM_TOKEN_SECRET`/`VOTER_SALT` fallback — the per-host `VOTER_SALT` values
  differ, and the key is now computed on both sides, so a mismatch 404s every crop.
- (`e14-r1-archive` is not deployed pre-R2; it gets the same secret at the R2 cutover, step 7 above.)

| # | Step | Why safe |
|---|------|----------|
| 0 | Set the shared secret on `e14-poll` + publish box | prerequisite |
| 1 | `python -m scripts.rekey_crops --limit 50 --dry-run`, then `--limit 50` | smoke-tests secret+creds before bulk |
| 2 | Pause `publish-loop` | no new readable-key uploads slip in |
| 3 | `python -m scripts.rekey_crops --workers 32` (full server-side copy, ~tens of min) | old+new keys coexist → live app keeps serving, no downtime |
| 4 | `e14detector publish-reconcile --output-dir <OUTPUT_DIR>` | manifest now holds opaque keys |
| 5 | Deploy this code to `e14-poll` **and** the publish box; resume `publish-loop` | app serves opaque URLs; new uploads opaque |
| 6 | Verify: open a crop in a new tab → `/crops/<hmac>.png`, no mesa id; feed loads; spot-check `/acta` | gate before destruction |
| 7 | `python -m scripts.rekey_crops --delete-old` + `publish-reconcile` | removes the leaking originals |

**Rollback:** any time before step 7, redeploy the previous `e14-poll` code — the readable keys still
exist, so it serves fine. After step 7 the originals are gone (that's why delete is last, gated on
step 6). The `CopyObject`/`DeleteObject` calls are server-side (bytes stay in Tigris, no egress).

## Capacity (runoff ramp) — WS-G

- Web concurrency: bump `e14-poll` to `--workers 2` (both vCPUs share one page cache for the ~730MB
  DB) and `fly scale count N -r bog,dfw` for horizontal read replicas (each pulls the snapshot at boot).
- Aurora R2 can pause/cold-start; SQS absorbs the gap so no vote is lost during a resume.
- Watch: SQS queue age + DLQ (CloudWatch alarms ship with `E14VoteStackR2`), `/health`, Tigris.
- VLM stays OFF (CV-only) throughout.
