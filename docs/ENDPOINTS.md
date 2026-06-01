# Reverse-engineered API — Registraduría E-14 visor (2026 Presidencial)

Source site: `https://divulgacione14presidente.registraduria.gov.co`
(Angular SPA "E14VisorCiudadano"). All findings below were verified live on
2026-05-31 (election day, first round).

## TL;DR — the path we actually use

The SPA can read its data from **static JSON + static PDF files on the Akamai
CDN** (its `useJsonSource`/divipol fallback). This needs **no GraphQL, no
Cognito, no SigV4, no reCAPTCHA** — just the right TLS fingerprint + Akamai
cookies. This is the scraper's primary (and only required) path.

1. **Prime** Akamai cookies: `GET /` → sets `ak_bmsc` (and later `bm_sv`).
2. **Universe**: `GET /assets/temis/divipol_json/allTransmissionCodes.json`
   → every acta nationally (120,872 nodes on 2026-05-31), each with its geo
   codes and PDF filename.
3. **Download**: `GET /assets/temis/pdf/{dep}/{muni}/{zona}/{puesto}/{mesa}/PRE/{expectedName}`

### ⚠️ Akamai bot management
- The host fingerprints the client's TLS/JA3. Plain `requests`/urllib3 and
  default `curl` (HTTP/2) are **silently dropped** (connection reset / read
  hang). A real browser cipher/ALPN profile is required.
- **Solution in code:** `curl_cffi` with `impersonate="chrome"` (see
  `e14/session.py`). `curl --compressed` with full browser headers also works.
- The JSON/PDF asset paths additionally require the `ak_bmsc`/`bm_sv` cookies
  set by loading the homepage first. Re-prime on connection-drop / 403.

## Static data files (`/assets/temis/divipol_json/`)

| File | Contents |
|---|---|
| `allTransmissionCodes.json` | **The download universe.** `data.{status11,status3}.nodes[]` |
| `allCorporations.json` | `idCorporationCode "001" → nameCorporation "PRESIDENTE", acronym "PRE", level "NAC"` |
| `departmentsTree.json` | full depto→muni→zona→stand tree (countTable per stand) |
| `allDepartments.json`, `CorpIndexAndMap.json`, `allMviewGetProgressBy*.json` | progress/aux views (not needed) |

### `allTransmissionCodes.json` node shape
```json
{ "idTransmissionCode":"4813912", "numberStand":"001",
  "expectedName":"3a1259d7…c087c5.pdf", "idTransmissionCodeStatus":11,
  "idCorporationCode":"001", "idStand":"019800401", "standCode":"01",
  "idZoneCode":"98", "idDepartmentCode":"01", "municipalityCode":"004" }
```
- `idTransmissionCodeStatus`: 11 = normal (120,555), 3 = other (265). Both carry
  a usable `expectedName` and a downloadable PDF.
- `expectedName` is content-addressed (`sha256.pdf`) — it IS the canonical
  server filename (store it in provenance).

## Acta PDF URL template (confirmed by download)

```
https://divulgacione14presidente.registraduria.gov.co/assets/temis/pdf/
   {idDepartmentCode:0>2}/{municipalityCode:0>3}/{idZoneCode:0>3}/
   {standCode:0>2}/{numberStand:0>3}/PRE/{expectedName}
```
- Zero-padding (verified against the live 404-vs-PDF behavior and the JS
  mapper in `chunk-QBZORZXG.js`): **dep→2, muni→3, zona→3, puesto→2, mesa→3.**
  (Getting zona padding wrong — e.g. `98` instead of `098` — returns the
  491-byte Angular `index.html` fallback with HTTP 200, NOT a 404. The
  downloader treats a non-`%PDF-` 200 body as a **soft failure**.)
- Optional `?uuid=<random>` query param (cache-buster) — not required.
- `PRE` = corporation acronym for idCorporationCode `001` (Presidente).
- Response: `Content-Type: application/pdf`, ~95–100 KB, **3 pages**, each a
  single scanned image (no embedded text → image OCR downstream).

## Acta variant — CONFIRMED **E-14 de DELEGADOS**

Despite the GraphQL/JSON operation being named *TransmissionCodes*, the served
PDF header reads:

> **ACTA DE ESCRUTINIO DE LOS JURADOS DE VOTACIÓN — DELEGADOS — E-14**
> Elección Presidencia y Vicepresidencia de la República, Mayo 31 de 2026

i.e. this site serves the **E-14 de Delegados (claveros)** actas — the priority
target in `init.md`. The printed header (DEPARTAMENTO/MUNICIPIO/ZONA/PUESTO/
MESA/LUGAR) matches the JSON codes exactly; the barcode encodes the form number
+ page index (`…0103`, `…0203`, `…0303` for pages 1/2/3 of 3); `No. Form`,
`KIT`, `Civ` print per page — all as predicted in `init.md`.

## Backend GraphQL (NOT used — documented for completeness / fallback)

- Endpoint: `https://apx2e14awsprodpresidencia.prdtpssas.com/graphql`
  (AWS **AppSync**, region `us-east-2`).
- Auth: **AWS_IAM SigV4** via an **anonymous Cognito Identity Pool**
  `us-east-2:58326cd4-70d8-4b4c-bd34-adc55fa72dc3`
  (`GetId` → `GetCredentialsForIdentity` → sign POST). Implemented in
  `e14/auth.py` + `e14/api.py` and validated (`{"data":{"__typename":"Query"}}`).
- **Why unused:** the live data resolvers (`allTransmissionCodes`,
  `departmentsTree`, `allCorporations`, …) returned *"Cannot return null for
  non-nullable type"* for the anonymous role and the mview-backed queries are
  slow/throttled. The static JSON is the same data, faster, unauthenticated,
  and is what the SPA itself falls back to. Keep the GraphQL client only as a
  staleness fallback if the static JSON stops updating.

## Robots / politeness
- We are a guest on a public-interest service. Defaults: global rate limit
  (8 req/s), bounded concurrency (6 workers), exponential backoff on
  429/5xx/timeouts, descriptive UA + contact (`E14_CONTACT`). Prefer off-peak.
