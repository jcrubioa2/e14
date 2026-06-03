# Plan: Bulletproof vote ingestion on AWS (CDK infra, Fly compute stays)

> Checklist for a fresh agent. Each `[ ]` is a discrete, verifiable step.
> **Environment note:** any new terminal is already authenticated to the user's AWS account
> (`aws sts get-caller-identity` works without setup). You can run `cdk` / `aws` directly.

## Context

The platform is now **crowd-only anonymized swipe voting** (see `plans/pending/swipe-voting.md`,
commit `a8a2dcc`). An election is **imminent (<6 weeks)** with an expected peak of **50–500 votes/s**
on a day that **cannot be replayed** — every vote matters.

Today the whole app runs on **one Fly machine, one uvicorn worker, writing one `community.sqlite`**
behind a `threading.Lock` (`e14detector/community.py`, `e14detector/webapp.py`). That single SQLite
writer on a single Fly volume is the real fragility under an election spike.

**Decision (settled with the user):**
- **Compute stays on Fly** for this election (lowest deadline risk). Consolidate compute to AWS only
  *after* the election — out of scope here.
- **Durable state moves to AWS, provisioned entirely via AWS CDK (Python):**
  - **SQS** (standard + dead-letter) absorbs votes so they survive even if a web machine, the worker,
    or the DB is down/hanging. This is the bulletproofing.
  - **Aurora Serverless v2 PostgreSQL** replaces `community.sqlite` — durable dedup, atomic counts,
    billboard via `ORDER BY`, analytics in plain SQL. Accessed from Fly via the **RDS Data API**
    (HTTPS + IAM, no VPC peering, no public DB port).
- **Crops stay on Tigris + Cloudflare** (near-zero egress). **Do NOT move images to S3** — egress cost
  on the highest-bandwidth tier. Not part of the CDK stack.
- **`results.sqlite` (read-only acta/crop index) is unchanged** (`e14detector/dbsync.py`).

### Core rules (non-negotiable)
1. **No vote is lost.** Web path validates then enqueues; the synchronous DB write goes away.
2. Preserve all existing anti-fraud exactly: `voter_token` daily IP hash, signed form token, honeypot,
   rate-limit bucket, Turnstile (`webapp.py` `api_vote()` / `bot_check()`).
3. Preserve dedup semantics: `UNIQUE(field_key, voter_token)` per direction → `ON CONFLICT DO NOTHING`.
4. Tallies become **eventually consistent**: `/api/vote` returns an **optimistic** count
   (`current + 1`), reconciled once the worker commits. Acceptable for crowd voting.

---

## Phase 0 — Prereqs & naming
- [ ] Confirm AWS identity + region: `aws sts get-caller-identity`; choose a region near the Fly
      region (DFW → `us-east-1` or `us-west-2`) and use it consistently. Record it in the CDK stack.
- [ ] Decide resource prefix `e14-vote-` for queue/cluster/secret/IAM names.
- [ ] Confirm `boto3` is present (it is, used in `e14detector/publish.py`); it will be promoted to a
      required serve/worker dependency in Phase 6.

## Phase 1 — CDK infrastructure (new `infra/` project, Python CDK)
- [ ] Create `infra/` with a Python CDK app (`app.py`, `cdk.json`, `requirements.txt` pinning
      `aws-cdk-lib`, `constructs`). Add `infra/README.md` with deploy/destroy commands.
- [ ] `cdk bootstrap` the target account/region (idempotent).
- [ ] **VPC**: minimal VPC for Aurora (2 AZs, isolated subnets; no NAT needed — Data API is the access
      path). Aurora must live in a VPC even when reached via Data API.
- [ ] **Aurora Serverless v2 PostgreSQL cluster**:
      - Engine `aurora-postgresql`; Serverless v2 with `minCapacity` 0 (scale-to-zero) or 0.5 ACU,
        `maxCapacity` sized for the spike (e.g. 4–8 ACU).
      - **Enable the RDS Data API** (`enableDataApi: true`).
      - Master credentials managed in **Secrets Manager** (CDK-generated secret).
      - Default database name `e14`.
- [ ] **SQS standard queue** `e14-vote-events` with a **dead-letter queue** `e14-vote-events-dlq`
      (redrive `maxReceiveCount` ~5), visibility timeout ≥ worker batch processing time (e.g. 60s),
      message retention 14 days.
- [ ] **IAM user** (programmatic, for Fly) with a least-privilege policy scoped to:
      `sqs:SendMessage|ReceiveMessage|DeleteMessage|GetQueueAttributes` on the queue;
      `rds-data:ExecuteStatement|BatchExecuteStatement` on the cluster;
      `secretsmanager:GetSecretValue` on the DB secret.
- [ ] **Stack outputs**: `SQS_QUEUE_URL`, `AURORA_CLUSTER_ARN`, `AURORA_SECRET_ARN`, `AWS_REGION`,
      and the IAM access key id (write secret access key to Secrets Manager / print once securely).
- [ ] (Nice-to-have) CloudWatch alarms: DLQ depth > 0, SQS age-of-oldest-message high, Aurora ACU max.
- [ ] `cdk deploy`; verify with `aws sqs get-queue-attributes` and a trivial `aws rds-data
      execute-statement --sql 'select 1'`.

## Phase 2 — Postgres schema
- [ ] Add `infra/schema.sql` (or a small migration runner using `rds-data`) recreating the
      `community.sqlite` tables in Postgres, mirroring `e14detector/community.py`:
      - `flags(field_key text, voter_token text, created_at timestamptz, UNIQUE(field_key, voter_token))`
      - `appeals(...)` same shape (separate direction counter)
      - `rate_buckets(voter_token text primary key, tokens double precision, updated_at timestamptz)`
      - `cid_index(cid text primary key, field_key text, crop_path text)`
      - any `field_state` columns still in use post-VLM-removal (check current `community.py`).
- [ ] Add indexes: `flags(field_key)`, `appeals(field_key)`.
- [ ] (Optional, billboard) a materialized view `hot_crops_mv` or a cached query for
      `ORDER BY strange DESC, (strange+good) DESC LIMIT :n`.

## Phase 3 — Data layer port (`community.py` SQLite → Postgres via Data API)
- [ ] Introduce a `rds-data` (boto3 `client('rds-data')`) backend behind the existing
      `community.py` API so callers (`webapp.py`) are unchanged. Keep method names:
      `record_flag/record_appeal`, `counts_among`, `hot_crops`, `allow` (rate bucket),
      `register`/`resolve` (cid), `voter_token` (unchanged — pure hash).
- [ ] Replace `INSERT OR IGNORE` with `INSERT ... ON CONFLICT DO NOTHING`.
- [ ] Replace `COUNT(DISTINCT voter_token)` tally queries; `counts_among()` does a batched query.
- [ ] Drop the `threading.Lock`/WAL coordination — Postgres handles concurrency.
- [ ] cid registration on feed (`register`) stays a **direct synchronous** Data API write (small;
      not a vote, must exist before its crop can be voted/resolved).

## Phase 4 — Vote write path → enqueue (`webapp.py` `api_vote`, ~line 1142)
- [ ] After all validation + cid resolution succeed, **publish** to SQS a JSON message
      `{field_key, voter_token, direction: "good"|"strange", ts}` instead of writing to the DB.
- [ ] Return **optimistic** tallies: read current `{good, strange}` for the field (Postgres or a
      short-TTL in-process cache) and return `current + 1` for the voted direction.
- [ ] Keep rate-limit / honeypot / form-token / Turnstile checks **before** enqueue, unchanged.
- [ ] Reads (`/api/feed` ~1072, `/api/billboard` ~1103, `/api/acta-crops`, `/c/{cid}` ~1023) query
      Postgres directly via the ported `community.py`.

## Phase 5 — Worker (drain SQS → Postgres)
- [ ] New module `e14detector/vote_worker.py` + entrypoint: long-poll SQS (boto3), for each message
      `INSERT ... ON CONFLICT DO NOTHING` into `flags`/`appeals`, then delete the message. Process in
      small batches; rely on the DLQ for poison messages.
- [ ] Idempotent by construction (dedup constraint), so SQS at-least-once redelivery is safe.
- [ ] Add structured logging + a simple processed/failed counter.

## Phase 6 — Fly deploy config
- [ ] `fly.toml`: define two **process groups** — `web` (uvicorn, autoscaling /
      `min_machines_running` raised for the election) and `worker` (runs `vote_worker`). Remove the
      "single worker to serialize SQLite" constraint and the `community.sqlite` volume mount.
- [ ] `pyproject.toml` / `Dockerfile`: promote `boto3` to a required serve/worker dependency (already
      present for Tigris publishing). No Postgres driver needed if using Data API; otherwise add
      `asyncpg`/`psycopg`.
- [ ] Set Fly secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `SQS_QUEUE_URL`,
      `AURORA_CLUSTER_ARN`, `AURORA_SECRET_ARN`.
- [ ] Leave `results.sqlite` + `dbsync.py` untouched.

## Phase 7 — Images (no change, verify only)
- [ ] Confirm crops still serve from Tigris via `E14_CDN_BASE_URL` (`e14detector/publish.py`) with
      Cloudflare in front. **Do not add S3.**

## Phase 8 — Tests & resilience verification
- [ ] Port/extend `tests/test_community.py` against Postgres: dedup (double vote = one count),
      per-direction independence, rate bucket, cid resolution, anonymized tallies.
- [ ] **Idempotency**: enqueue the same vote message twice → exactly one row, counts unchanged.
- [ ] **Resilience (the whole point)**: load-generate ~500 votes/s; mid-run **kill the worker** and
      separately **pause Aurora / revoke DB access**. Assert `/api/vote` keeps returning 200 (SQS
      absorbs), then on recovery the worker drains with **zero lost votes** and tallies converge.
- [ ] **Load**: `feed → vote → billboard` loop at target rate; confirm image requests hit Cloudflare
      (Fly CPU stays low) and billboard latency stays flat.

## Phase 9 — Cutover & rollback
- [ ] Stand up AWS infra + deploy to a Fly staging app first; smoke-test the full loop.
- [ ] Cut over production; watch SQS queue depth, DLQ, and Aurora ACU.
- [ ] **Fallback if the Postgres port can't land in time**: keep `community.sqlite` on the single Fly
      machine, add SQS + a single serial drain worker, and add **Litestream** replicating SQLite to
      Tigris for durability. Less work; single DB-holding machine remains the limit. Use only if forced.

## References
- Architecture rationale + cost analysis: `~/.claude/plans/i-m-thinking-of-migrating-validated-brooks.md`
- Current voting impl: `e14detector/webapp.py`, `e14detector/community.py`, `e14detector/config.py`
- boto3 precedent (Tigris): `e14detector/publish.py`
- Prior product checklist: `plans/pending/swipe-voting.md`
