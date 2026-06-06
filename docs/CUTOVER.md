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
| Crops | `crops/<file>` | `crops/r2/<file>` |
| Universe snapshot (local) | `data/universe_snapshot.json` | `data/r2/universe_snapshot.json` |
| Vote backend (Aurora + SQS) | retired after bake (verdicts baked into the archive snapshot) | fresh `E14VoteStackR2` |
| Tigris bucket | one bucket, round-prefixed (NOT a second bucket) | same bucket |

The bucket keys for R1 never move (the `r1` round maps to the legacy un-prefixed keys), so standing
up R2 moves zero R1 data.

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
   ./scripts/deploy.sh r1-archive
   ```
   Verify it serves the frozen R1 at `https://e14-r1-archive.fly.dev` (then attach the
   `primera-vuelta.<domain>` cert/DNS if desired, and set `E14_SITE_URL` to it).
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

## Capacity (runoff ramp) — WS-G

- Web concurrency: bump `e14-poll` to `--workers 2` (both vCPUs share one page cache for the ~730MB
  DB) and `fly scale count N -r bog,dfw` for horizontal read replicas (each pulls the snapshot at boot).
- Aurora R2 can pause/cold-start; SQS absorbs the gap so no vote is lost during a resume.
- Watch: SQS queue age + DLQ (CloudWatch alarms ship with `E14VoteStackR2`), `/health`, Tigris.
- VLM stays OFF (CV-only) throughout.
