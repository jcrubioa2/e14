# E-14 Scraper — progress checklist

Goal: download **all** E-14 de Delegados acta PDFs (national, 2026 presidential),
resumably, with provenance. Scraper only — no OCR/analysis (see `init.md`).

## Done
- [x] Recon: reverse-engineered the site (Angular SPA + Akamai + AppSync GraphQL).
- [x] Found the clean path: **static JSON + static PDF on the CDN** (no auth).
- [x] Beat Akamai TLS fingerprinting with `curl_cffi` (impersonate chrome) + cookie prime.
- [x] Pinned the PDF URL template + zero-padding (dep2/muni3/zona3/puesto2/mesa3/PRE/file).
- [x] **Confirmed variant = E-14 de DELEGADOS** (read the acta header image).
- [x] `e14/` package: config, session, universe, manifest, downloader, cli (+ auth/api fallback).
- [x] Built universe → `data/mesa_universe.csv` (**120,872 actas, 34 departments**).
- [x] Test downloads (Bogotá + Antioquia slices): valid `%PDF`, SHA-256 + provenance, resume verified.
- [x] `docs/ENDPOINTS.md` written.

## Next
- [ ] **Full national run**: `e14 download --rate 8 --concurrency 6` (~12 GB, ~4 h est).
      Run off-peak; monitor `e14 stats`.
- [ ] `e14 download --retry-failed` until `failed.csv` is empty / only genuine gaps remain.
- [ ] Spot-check a few departments: header codes vs filename codes; barcode page indices.
- [ ] (Optional) cron a daily `--refresh` + incremental run while results keep updating
      (status counts grow as more mesas are scrutinized/published).
- [ ] Hand off `data/actas/` + `data/manifest.db` to the OCR stage.

## Commands
```bash
.venv/bin/python -m e14.cli build-universe          # refresh the universe CSV
.venv/bin/python -m e14.cli estimate                # volume + runtime projection
.venv/bin/python -m e14.cli download --depto 16 --limit 50   # test slice
.venv/bin/python -m e14.cli download                # full national (resumable)
.venv/bin/python -m e14.cli download --retry-failed # re-attempt failures only
.venv/bin/python -m e14.cli stats                   # manifest summary
```

## Notes / watch-outs
- Wrong zona padding → server returns 200 + Angular `index.html` (491 B), not 404.
  The downloader rejects any non-`%PDF-` 200 as a soft failure — good.
- `status3` nodes (265) vs `status11` (120,555): both downloadable; included.
- Data still updating on election night — re-run with `--refresh` to pick up new actas.
- Be polite: keep rate ≤ ~8 req/s; back off on 429/5xx (already automatic).
