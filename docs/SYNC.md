# `e14 sync` — the one way to run the incremental sync

One command group, with the count-model consistency rules **baked into the code path** so they
can't be forgotten. It replaces the scattered publisher scripts and the dozen separate verbs
(`publish-crops` / `publish-db` / `publish-loop` / `publish-reconcile` / `pull-db` / …) that used
to require operators to remember the rules in the right order. Those primitives still exist and
still work; `e14 sync` is the orchestrator you should reach for.

CV-only: no VLM stage runs anywhere in the sync path.

## The four verbs

```bash
e14 sync status     # print the count chain + cobertura + both backlogs (read-only)
e14 sync verify     # assert the invariant chain; nonzero exit on any inversion (cron / pre-lock)
e14 sync run        # the one safe publisher loop: refresh universe → upload crops → publish frontier
e14 sync restore    # resume on a fresh/crashed machine: rebuild manifest from bucket + pull DB
e14 sync fleet      # multi-machine: points to the detector fleet verbs (see below)
```

Common flags: `--output-dir` (default `data/detector`), `--bucket` (`$BUCKET_NAME`),
`--cdn-base` (`$E14_CDN_BASE_URL`).

### Typical flows

```bash
# Fresh machine (or unsure of local state) → publish:
e14 sync restore --output-dir <OUT>          # reconcile manifest from the bucket + pull the live DB
e14 sync run     --output-dir <OUT>          # continuous: universe + crop delta + frontier DB

# One-shot cycle (then it verifies the chain and exits):
e14 sync run --once --output-dir <OUT>

# Check the counts from a terminal, or gate a cron:
e14 sync status
e14 sync verify || echo "chain inconsistent — investigate"
```

## The rules it enforces (always on, not flags)

| Rule | What it guarantees |
|------|--------------------|
| **lock-aware** | never overwrites a `db/lock.json`-locked round unless you pass `--allow-locked` |
| **frontier-only** | publishes only actas whose crops are all uploaded (`only_uploaded`), so a served acta's crop never 404s |
| **shrink-guard** | refuses a DB that lost >50% of its actas (wrong `--output-dir` / stub), unless `--allow-shrink` |
| **chain-stamp** | every publish stamps the count-model reconciliation block into `db/latest.json` (the single reconciliation record the admin + `/transparencia` render) |
| **universe-refresh** | `run` re-fetches the registraduría universe each cycle so the cobertura denominator stays honest (shrink-guarded) |
| **verify-first** | a one-shot `run` re-checks the invariant chain before returning |

## The count model (what the chain means)

Non-increasing, top to bottom — any inversion is an alarm:

```
total_global ≥ mesas_informadas ≥ downloaded ≥ crops_uploaded ≥ sqlite_served == published
```

- **total_global** / **mesas_informadas** — registraduría (`allTransmissionCodes.json`), via
  `e14 refresh-universe` → `data/universe_snapshot.json`.
- **downloaded** — manifest `status='done'`. **crops_uploaded** — the uploaded frontier.
- **sqlite_served** — `COUNT(documents)` in the served DB. **published** — pointer `n_docs`.
- **cobertura** = served / informadas. **backlog de ingesta** = informadas − served.
  **backlog de reporte** = total_global − informadas.

Full rationale: [ARCHITECTURE.md](ARCHITECTURE.md) and the count-model notes.

## Old → new mapping

| Old script / verb | Use instead |
|-------------------|-------------|
| `publish_supervisor.sh`, `publish_dept_crops.sh`, `e14detector publish-loop` | `e14 sync run` |
| `crop_progress.sh`, `publish_status_by_dept.sh` | `e14 sync status` |
| `e14detector publish-reconcile` + `pull-db` | `e14 sync restore` |
| (new) | `e14 sync verify` |

## Multi-machine (fleet)

The crop **fleet** (multiple worker machines sharing a department queue) still lives in the
detector CLI — `e14 sync fleet` prints the pointers:

```bash
python -m e14detector.cli fleet-init | fleet-status | fleet-schedule | fleet-complete
python -m e14detector.cli pull-fleet | publish-fleet
```

See [MULTI_MACHINE.md](MULTI_MACHINE.md) for the worker-bootstrap details. Each worker still
publishes through the same `e14 sync`-enforced rules, so the fleet can't desync the served counts.
