# E-14 Acta Scraper — 2026 Colombian Presidential Election

Downloads the official **E-14 de Delegados** actas de escrutinio (polling-station
tally sheets) for the 2026 presidential election from the Registraduría's public
visor, with full provenance, ready for a downstream OCR/anomaly pipeline.

**Scraper only** — no OCR, vote extraction, or analysis (see `init.md`).

## How it works (see `docs/ENDPOINTS.md`)

The Registraduría SPA falls back to **static JSON + static PDF files on its
Akamai CDN** — no GraphQL/Cognito/SigV4/reCAPTCHA needed:

1. Prime Akamai cookies via the homepage (`curl_cffi` impersonating Chrome,
   because Akamai drops non-browser TLS fingerprints).
2. Fetch `allTransmissionCodes.json` → the full national acta list
   (~120k actas, each with geo codes + PDF filename).
3. Download each acta from
   `/assets/temis/pdf/{dep:2}/{muni:3}/{zona:3}/{puesto:2}/{mesa:3}/PRE/{file}`.

Provenance (SHA-256, HTTP status, content-type/length, UTC timestamp, source
URL, server filename, geo codes) is recorded in a SQLite manifest. Runs are
resumable and idempotent.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -e .      # or: curl_cffi requests tenacity tqdm
```

## Quick start (Makefile)

```bash
make setup          # venv + deps (one time)
make run            # download, resuming where it left off (safe to re-run)
make fresh          # wipe everything and re-download (asks for confirmation)
make stats          # progress: done / failed / skipped + GB
make retry          # re-attempt only failed actas
make package        # -> dist/by_department/*.zip + SHA256SUMS + index.csv
make verify         # check downloaded PDFs against SHA256SUMS
make help           # list all targets
```
Tune throughput: `make run CONCURRENCY=96 RATE=200`. Defaults: 64 workers / 120 req/s.

## Usage (direct CLI)

```bash
.venv/bin/python -m e14.cli build-universe                  # data/mesa_universe.csv
.venv/bin/python -m e14.cli estimate                        # volume + runtime
.venv/bin/python -m e14.cli download --depto 16 --limit 50  # test slice
.venv/bin/python -m e14.cli download                        # full national (resumable)
.venv/bin/python -m e14.cli download --retry-failed         # re-attempt failures
.venv/bin/python -m e14.cli stats                           # manifest summary
.venv/bin/python -m e14.cli package                         # per-department zips + SHA256SUMS
```

Filters: `--depto`, `--muni`, `--limit`. Throughput: `--rate` (req/s, default 20),
`--concurrency` (default 16). `--auto-retry N` re-runs failures N times after the
main pass (default 2 — self-healing for unattended runs). `--force` re-downloads.
Set `E14_CONTACT` for the UA contact string.

### Run it overnight (resumable, self-healing)

```bash
# leave running in a terminal; safe to Ctrl-C and re-run — it resumes.
.venv/bin/python -m e14.cli download --concurrency 24 --rate 30 --auto-retry 5 \
    2>&1 | tee logs/run_$(date +%Y%m%d).log
```
- Live `tqdm` progress bar; a **per-acta JSON line** is written to
  `logs/results.jsonl` (success *and* failure, with sha256/http/bytes/url).
- At the end it prints a FINAL SUMMARY with a failure-reason breakdown and writes
  `data/failed.csv`. Re-run with `--retry-failed` (or rely on `--auto-retry`).
- To detach fully: `nohup … &` or run inside `tmux`/`screen`.

### Raw vs packaged
- **Raw, file-by-file** lives in `data/actas/...` (original PDF bytes, unmodified).
- **`package`** is additive — it reads the raw tree and writes shareable
  `dist/by_department/{dep}.zip` + `dist/SHA256SUMS.txt` (+ index/dictionary/
  provenance docs) without touching the raw files. Verify anytime:
  `cd data/actas && sha256sum -c ../../dist/SHA256SUMS.txt`.

### Why numeric folders + a dictionary (not text names)
Folder paths use the official **DIVIPOL numeric codes**
(`{dep}/{muni}/{zona}/{puesto}/`) — the canonical key for joining these images to
the Registraduría's published *numeric* results, and stable/encoding-safe for
torrents. Human readability ships alongside as lookup tables (`e14 dictionary`,
auto-included by `package`):
- **`index.csv`** — one row per acta: dep/muni/zona/puesto **names** + *Lugar* +
  mesa + file path + URL. Open in Excel and search "MEDELLÍN", "ABEJORRAL", etc.
- **`divipol_dictionary.csv`** — every puesto code → names (+ `Lugar`, mesa count).

## Output layout

```
data/
  mesa_universe.csv         # enumerated (dep,muni,zona,puesto,mesa,expected_name,…)
  manifest.db               # SQLite source of truth (status + provenance per acta)
  failed.csv                # exportable view of failures
  actas/{dep}/{muni}/{zona}/{puesto}/E14_PRE_{dep}_{muni}_{zona}_{puesto}_{mesa}_delegados.pdf
logs/e14.log
docs/ENDPOINTS.md           # reverse-engineered API + verified field mapping
docs/ARCHITECTURE.md        # the live platform: Fly web + SQLite corpus + SQS/Lambda/Aurora votes
docs/PUBLISHING.md          # sync a detector machine's crops + DB to the deployment (publish-reconcile/-loop)
docs/SEEDING.md             # labeling samples into public "strange" seeds
checklist.md                # living progress tracker
```

## Provenance / ethics

Electoral-transparency work: pull only from the official source, preserve
original bytes + SHA-256, keep the chain of custody clean. Anomalies downstream
are not proof of fraud; OCR error dominates raw flags; fake actas circulate.
`expectedName` is the server's content-addressed (`sha256.pdf`) filename — kept
in the manifest so each file traces back to the authentic original.

## Modules

| Module | Role |
|---|---|
| `e14/session.py` | Akamai-aware HTTP (curl_cffi Chrome impersonation, cookie prime, retries) |
| `e14/universe.py` | fetch/parse `allTransmissionCodes.json` → `ActaRecord`s + CSV |
| `e14/manifest.py` | SQLite manifest + provenance |
| `e14/downloader.py` | threaded download, atomic writes, soft-failure detection |
| `e14/cli.py` | command-line interface |
| `e14/config.py` | reverse-engineered constants + PDF URL builder |
| `e14/auth.py`, `e14/api.py` | **optional** GraphQL/SigV4 fallback (unused by default) |
| `e14/util.py` | retry/backoff, rate limiter, progress (no hard tqdm/tenacity dep) |
```
