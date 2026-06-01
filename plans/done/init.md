# Project: Registraduría E‑14 Acta Scraper (Colombia, Presidencial 2026)

## Goal

Build a robust, resumable scraper that downloads the official **E‑14 actas de escrutinio**
(polling‑station tally sheets) for the 2026 Colombian presidential election from the
Registraduría Nacional del Estado Civil, and stores each acta as a PDF/image alongside
structured metadata and provenance, ready for a downstream OCR + anomaly‑analysis pipeline.

**This task is the scraper only.** OCR, vote extraction, and statistical analysis are separate
downstream stages — do not build them here, but produce output in a shape that makes them easy.

## Context

- The Registraduría is the **only authorized source** for authentic E‑14 actas
  (`www.registraduria.gov.co`). AI‑fabricated fake actas are known to circulate, so we must
  pull exclusively from the official site and record cryptographic provenance for every file.
- Actas are organized as a geographic drill‑down:
  **Departamento → Municipio → Zona → Puesto → Mesa.**
- The site exposes (at least) two acta variants we may want:
  - **E‑14 de Delegados** (a.k.a. claveros) — the priority target.
  - **E‑14 de Transmisión** — optional second variant; useful for image‑vs‑image checks later.
- Known navigation path on the site: `Resultados Electorales` →
  `Actas E‑14 de Delegados` → `Consulta Actas E‑14C (claveros)` → select
  departamento/municipio/zona/puesto/mesa.
- The Registraduría also publishes the **digitized numeric results** (preconteo +
  escrutinio) separately. We do NOT scrape those here, but the manifest keys must align with
  them (same depto/muni/zona/puesto/mesa codes) so a later stage can join image ↔ official number.

## Strategy: hit the API, not the rendered HTML

The consulta UI is almost certainly a JavaScript front end backed by JSON endpoints that feed
the cascading dropdowns and return the acta file URL. **Step 1 is reconnaissance, not coding:**

1. Open the consulta page in a browser, open DevTools → Network, and perform one manual
   drill‑down to a single mesa whose acta you can see (use the two sample mesas below to verify).
2. Capture the requests that populate each dropdown level and the request that returns the
   final acta (PDF or image). Document the exact URL templates, query params, headers, and any
   tokens/cookies required.
3. Prefer those JSON/file endpoints over HTML scraping or browser automation. Fall back to a
   headless browser (Playwright) **only** if the endpoints are protected in a way that requires
   a real session.

Record everything you discover in `docs/ENDPOINTS.md` before writing the downloader.

## Known facts from sample files (verify, don't assume)

Two confirmed sample mesas (use as ground truth for the recon step):

| Departamento | Municipio | Zona | Puesto | Mesa | Lugar |
|---|---|---|---|---|---|
| 16 - Bogotá D.C. | 001 | 17 | 01 | 002 | La Concordia |
| 01 - Antioquia | 001 - Medellín | 13 | 04 | 015 | I.E. Villa de la Candelaria |

Observed on the acta itself (use to validate your metadata mapping):
- A header block printing `DEPARTAMENTO / MUNICIPIO / ZONA / PUESTO / MESA / LUGAR`.
- A `No. Form`, a `KIT`, and a `Civ` number per page.
- A **barcode** and a **QR code**. The barcode encodes the form number and page indices —
  e.g. samples ended in `...0103`, `...0203`, `...0303` for "page 1/2/3 of 3". Decode the
  barcode with `pyzbar`/`zxing` and **empirically confirm** which fields map to depto/muni/
  zona/puesto/mesa/form/page against the printed header — do not trust a guessed layout.
- Each acta is **3 pages** (pages 1–2 carry vote data; page 3 is signatures/constancias).

> Note: filenames seen in the wild may be partially redacted/placeholder (e.g. `XXX` tokens),
> so treat the official endpoint response as authoritative for the canonical filename, not any
> local naming convention.

## Build the mesa universe

You need the complete enumerable list of `(depto, muni, zona, puesto, mesa)` tuples to iterate.
Two viable sources — prefer whichever is cleaner:

1. **Walk the API's own cascading dropdowns** (depto list → muni list → … → mesa list). This is
   self‑consistent with the acta endpoint and guarantees you only request mesas that exist.
2. **Divipol / open data**: the Registraduría's open‑data portal (`datos.gov.co`,
   `observatorio.registraduria.gov.co`) publishes the división política electoral and puesto/mesa
   listings. Cross‑check counts against (1).

Persist the universe to `data/mesa_universe.csv` before downloading, so runs are reproducible.

## Functional requirements

### Downloading
- For each mesa, fetch the **E‑14 Delegados** acta (and optionally Transmisión, behind a flag
  `--variant delegados|transmision|both`).
- Save the **raw original bytes unmodified** (do not re‑encode/recompress).
- Support `--depto`, `--muni`, and `--limit` filters for partial/test runs.

### Resumability & idempotency (critical at this scale)
- Maintain a **manifest** in SQLite (`data/manifest.db`) as the source of truth for what has been
  fetched, with a status per `(mesa, variant)`: `pending | done | failed | skipped`.
- On restart, skip anything already `done`; never re‑download unless `--force`.
- Writes must be atomic (download to a temp file, fsync, then rename).

### Politeness & resilience (we are a guest on a public‑interest service)
- Global rate limit (configurable, default conservative, e.g. a few requests/sec) plus bounded
  concurrency (default low, e.g. 4–8 workers).
- Exponential backoff with jitter on 429/5xx/timeouts (use `tenacity`); cap retries, then mark
  `failed` and move on. A separate `--retry-failed` pass re‑attempts the failed queue.
- Respect `robots.txt` and any published rate guidance; set a descriptive `User-Agent` with a
  contact. Prefer running off‑peak.
- Detect and handle soft failures: HTML error page returned with 200, empty/zero‑byte PDF,
  "acta no disponible" responses — these are `failed`, not `done`.

### Provenance (so results are defensible later)
For every file store: source URL, HTTP status, response `Content-Type` and `Content-Length`,
fetch timestamp (UTC), **SHA‑256 of the bytes**, file size, and the resolved
depto/muni/zona/puesto/mesa/variant. Keep the original server filename if one is provided.

### Output layout
```
data/
  manifest.db
  mesa_universe.csv
  actas/
    {depto}/{muni}/{zona}/{puesto}/
      E14_{depto}_{muni}_{zona}_{puesto}_{mesa}_{variant}.pdf
  failed.csv            # exportable view of failed (mesa, variant, reason)
logs/
docs/
  ENDPOINTS.md          # reverse-engineered API documentation
```

### Logging & observability
- Structured logs (one line per fetch: mesa key, status, bytes, latency).
- A live progress counter (e.g. `tqdm`) and a summary at the end: total / done / failed / skipped,
  bytes downloaded, elapsed, and est. remaining for full‑national.

## Suggested stack
- Python 3.11+, `httpx` (async) or `requests` + a worker pool.
- `tenacity` (retries), `sqlite3`/`sqlalchemy` (manifest), `pyzbar` + `pdf2image`/`pypdfium2`
  (barcode validation during recon), `tqdm`, `structlog`/stdlib `logging`, `typer`/`argparse` CLI.
- `playwright` only as the recon/fallback tool.

## Acceptance criteria
1. `docs/ENDPOINTS.md` documents the real endpoints and the verified field mapping.
2. A test run (`--limit 50` over the two sample mesas + a small slice) downloads valid PDFs,
   populates the manifest, and produces correct SHA‑256 + provenance rows.
3. Killing the process mid‑run and restarting resumes without re‑downloading completed mesas.
4. `--retry-failed` reprocesses only failed entries.
5. A dry estimate of full‑national volume and runtime is printed (mesa count × variants).

## Out of scope (do NOT build here)
OCR, digit extraction, sum/checksum validation, comparison against published numeric results,
and any statistical anomaly detection. Just produce clean, provenance‑tagged files + manifest.

## Open questions to resolve during recon
- Exact API endpoints, params, and any session/token requirement.
- Whether the acta is served as PDF or image, and at what resolution (resolution matters a lot
  for the later handwriting OCR — flag if low‑res).
- Authoritative mesa universe size and the cleanest source for it.
- robots.txt / ToS constraints and any rate guidance.

## Ethics note (carry into the downstream stages)
This is electoral‑transparency work. Anomalies are not proof of fraud, OCR error will dominate
raw flags, and fake actas circulate — so: pull only from the official source, preserve original
bytes + hashes, and keep the chain of custody clean so any later finding can be traced back to an
authentic, unmodified document.