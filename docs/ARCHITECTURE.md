# Architecture — the public vote-counting platform

How the live site (`veeduria-ciudadana-elecciones-colombia-2026.com`) is put together: a
read-mostly web app on Fly, a static detector corpus served from SQLite, anonymized crop images
on a CDN, and a durable vote pipeline on AWS. The design splits **static read data** from
**live mutable votes** so each can use the right store and fail independently.

---

## TL;DR — the shape

```
        detector machine (offline)                  AWS us-east-1 (durable votes)
        +----------------------+                    +---------------------------+
  PDFs->| detect -> crops + DB |                    |  SQS queue  -->  DLQ       |
        | publish-loop         |                    |     |                      |
        +----------+-----------+                    |     v                      |
                   | gz snapshot + crops            |  Lambda (drain)            |
                   v                                |     | RDS Data API         |
        +----------------------+                    |     v                      |
        | Tigris object store  |                    |  Aurora Postgres (votes)   |
        | crops/ + db/latest   |                    +------------^--------------+
        +----------+-----------+                                 | enqueue
              CDN  | (read)                                      | (SQS)
                   v                                             |
        +-------------------------------------------------+      |
visitors| Fly app  e14-poll  (web only, 1 machine)        |------+
   ---->| - serves slim results.sqlite from page cache    |
        | - /votar acta grid; /api/vote-batch -> SQS      |
        | - polls db/latest.json, atomic-swaps the corpus |
        +-------------------------------------------------+
```

Two data planes, deliberately separate:

- **Static corpus** (actas, candidate crops, geo): produced offline by the detector, shipped as
  a slim read-only **SQLite** snapshot + **crop images on a CDN**. Immutable per release; swapped
  atomically. Microsecond reads from the OS page cache.
- **Live votes** (mutable, every one irreplaceable): **SQS → Lambda → Aurora**. Concurrent,
  durable, idempotent.

---

## Compute — Fly app `e14-poll`

- **Web only.** One process group `web` (`uvicorn e14detector.asgi:app`), one
  `shared-cpu-2x / 2048 MB` machine, region `dfw`. A Fly volume holds the served SQLite snapshot
  + DB-sync state.
- Custom domain `veeduria-ciudadana-elecciones-colombia-2026.com` (+ `www`), TLS issued by Fly;
  `force_https`. Crops are **not** served by Fly — they come from the CDN.
- There is **no always-on worker** — the vote drain is a Lambda (it used to be a Fly `worker`
  process; that was retired). Idle vote-drain cost ≈ \$0.

> **Single web machine is the current SPOF** and the throughput ceiling for election day.
> Scaling to ≥2 machines (the slim DB fits) removes it; see the load-test note in the repo plan.

---

## Static corpus — slim SQLite served from the page cache

The detector working DB is ~2 GB (CV features, debug paths, etc.). The site never needs that, so
`dbsync.build_serving_db` produces a **slim snapshot**: candidate registry + geo +
`documents.n_candidates`, dropping the heavy columns/tables. ~2 GB → ~730 MB (gz ~46 MB).

- **Why SQLite, not Dynamo/Aurora for this:** the read shape is joins / `GROUP BY` / `DISTINCT`
  / cascading geo filters over a static corpus. SQLite in the OS page cache answers these in
  microseconds with zero network hops, and the whole file swaps atomically per release. A
  managed DB would add latency and a moving part for data that never changes between releases.
- **`n_candidates` precompute:** `/browse` used to join `documents ⨝ vote_fields` and
  `GROUP BY` (1.5M rows, ~3.6 s). The count is precomputed into a `documents` column, making
  `/browse` a documents-only query (~8 ms).
- **Publish/serve topology:** detector machine builds the snapshot and uploads it
  content-addressed to Tigris, then flips `db/latest.json`. The Fly app polls that pointer
  (`E14_DB_SYNC`, 60 s), downloads on change, and `os.replace()`s the served file (atomic), then
  **prewarms** the page cache. Full mechanics in [PUBLISHING.md](PUBLISHING.md).

## Crops — anonymized images on the CDN

Candidate vote-box crops are PNGs on Tigris, fronted by a CDN (`E14_CDN_BASE_URL`). The page
never exposes a crop's acta/path: it serves `/c/{cid}` where **`cid = HMAC(form_token_secret,
field_key)`** — opaque and un-reversible. Only cids the server has surfaced (and registered)
resolve; anything else 404s. This is what lets the swipe feed be anonymous (a voter can't tell
which acta/mesa a crop is from).

---

## Live votes — SQS → Lambda → Aurora

Every vote is irreplaceable, so the write path is built so **nothing is lost if the DB or
compute is momentarily down**. Provisioned entirely via **AWS CDK** in [`infra/`](../infra)
(`E14VoteStack`).

1. **Enqueue.** `/api/vote-batch` (and the single-crop `/api/vote`) validates, then
   `vote_queue.VotePublisher` **enqueues to SQS** and
   returns an *optimistic* tally. SQS absorbs the spike; the vote survives even if Aurora is down.
2. **Drain.** An SQS event-source mapping invokes the **`e14-vote-drain` Lambda**
   (`infra/lambda/handler.py`), which bulk-inserts into Aurora over the **RDS Data API**
   (`INSERT … ON CONFLICT DO NOTHING` — idempotent, so at-least-once redelivery is safe).
   Partial-batch responses = "a vote is done only after its insert commits"; a DB error
   redelivers the whole batch; malformed messages go to the **DLQ** after `maxReceiveCount` (60).
3. **Store.** **Aurora Serverless v2 Postgres** (Data API enabled, min 0.5 ACU warm floor, max 8)
   holds `flags` / `appeals` / `field_state` / `rate_buckets` / `cid_index`. Votes key on the
   **stable `field_key`** (acta identity: `document_id` + page/row/section), *not* a DB row id —
   so re-running the detector or republishing the corpus never orphans a vote.

**Why this split (vs. one DB):** the corpus is static and read-shaped (SQLite wins); votes are
mutable, concurrent, and analytic (Aurora wins). Separating them isolates failures (a corpus
republish can't touch votes; an Aurora hiccup can't take down reads) and lets each scale on its
own axis. DynamoDB was rejected for the *vote* side too — the tallies need `GROUP BY
COUNT(DISTINCT voter)` / top-N, which fight a KV store.

**Hardening knobs:** Lambda fan-out capped (`MaximumConcurrency = 20`) so a spike can't stampede
the Data API; DLQ + CloudWatch alarms (DLQ-not-empty, queue-age, Aurora ACU). Idle cost is the
Lambda (≈\$0) + the Aurora warm floor (~\$44/mo, the main line).

---

## The vote request path (and its defenses)

```
/votar  ──▶  swipe.html  ──▶  /api/acta-deck (one anonymized acta)  ──▶  /api/vote-batch  ──▶ SQS ──▶ Lambda ──▶ Aurora
                │                                                          ▲
                └─ Turnstile solve ─▶ /api/session ─▶ form_token ──────────┘
```

Defenses on the vote write path (`/api/vote-batch`, and the single-crop `/api/vote`), strongest first:

| Layer | Stops | Notes |
|---|---|---|
| **Form token** (`issue_form_token`, HMAC of session + time window) | blind POSTs that never loaded the page | rejects forged / too-fast submits; the bot gate that's always on |
| **Turnstile** (session-gate) | headless automation | invisible widget → `/api/session` verifies → mints the form token; one solve per session, no per-swipe friction. Behind `E14_TURNSTILE_ENABLED` |
| **Origin allowlist** (`E14_ALLOWED_ORIGINS`) | cross-site (CSRF) browser votes | rejects a present-but-foreign `Origin`; non-browser clients fall through to the controls below. CORS can't do this — it governs response *reads*, not requests |
| **Honeypot** + **rate limit** (per IP-hash token bucket) | naive bots / flooding | best-effort; rate generous post-Turnstile (`E14_RATE_*`) |
| **Opaque cid** | targeting/de-anonymizing | votes reference an HMAC cid, server-registered only |

Site-wide hardening: `/docs` `/redoc` `/openapi.json` hidden in prod (`E14_EXPOSE_DOCS`);
security headers on every response (`X-Content-Type-Options`, `X-Frame-Options` + CSP
`frame-ancestors 'none'`, `Referrer-Policy`, HSTS). API responses are already anonymized (no
`document_id` / location / candidate names — only `cid` + counts).

**Snappy voting:** `/api/vote-batch` is fired optimistically — on "Enviar" the UI shows a
thank-you toast and loads the next acta instantly (next deck preloaded), the request flying in
the background. Durability + idempotency make not-waiting safe (a vote is never lost or double-counted). The
blocking boto3 calls are offloaded to threads and the static `cid→field_key` map is cached, so
one web worker carries ~40 concurrent votes instead of ~1.

---

## Credentials

Two distinct AWS-shaped credential sets — easy to confuse, kept apart on purpose:

| Env names | Point at | Used by |
|---|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_ENDPOINT_URL_S3` / `BUCKET_NAME` | **Tigris** (object store) | crop/DB **publishing** + the CDN read path |
| `E14_VOTE_AWS_ACCESS_KEY_ID` / `…SECRET…` / `E14_VOTE_AWS_REGION` | **real AWS** (SQS + Aurora Data API) | the Fly web app's vote enqueue |
| *(none — execution role)* | **real AWS** | the vote-drain **Lambda** (IAM native) |

The split exists because a default boto3 client on Fly would otherwise pick up the Tigris
`AWS_*` keys and fail against real AWS. `vote_aws.vote_client` reads the `E14_VOTE_AWS_*` set.
In Lambda there are no Tigris keys, so the execution role supplies IAM natively — no `E14_VOTE_*`
needed there.

---

## Components at a glance

| Component | Where | Code |
|---|---|---|
| Web app (FastAPI/uvicorn) | Fly `e14-poll` | `e14detector/webapp.py`, `asgi.py` |
| Served corpus | Fly volume (SQLite) | `dbsync.py` (`build_serving_db`, reader) |
| Crops | Tigris + CDN | `publish.py`, served `/c/{cid}` |
| Vote enqueue | Fly web | `vote_queue.py` |
| Vote drain | AWS Lambda | `infra/lambda/handler.py` |
| Vote store | Aurora Postgres | `community_pg.py` (Data API) |
| Vote store (local/dev) | SQLite | `community.py` (`make_store` picks backend) |
| AWS infra (VPC, Aurora, SQS, Lambda, IAM) | CDK | `infra/e14_infra/vote_stack.py` |
| Swipe UI | template | `e14detector/templates/swipe.html` |

See also: [PUBLISHING.md](PUBLISHING.md) (sync a detector machine to the deployment),
[ENDPOINTS.md](ENDPOINTS.md) (the upstream Registraduría source), [SEEDING.md](SEEDING.md)
(seeding "strange" labels), and [`infra/README.md`](../infra/README.md) (CDK deploy + Turnstile
enable steps).
