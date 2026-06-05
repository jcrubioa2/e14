"""Static configuration reverse-engineered from the site's JS bundles and
confirmed end-to-end against the live site. See docs/ENDPOINTS.md.

PRIMARY data path is the site's own static-JSON + static-PDF source on the
Akamai CDN (the app's `useJsonSource`/divipol path) — no GraphQL, Cognito,
SigV4 or reCAPTCHA required. The GraphQL/SigV4 client (auth.py/api.py) is kept
as an optional live fallback only.
"""
from __future__ import annotations

import os

# Active election round (mirror of e14detector.config.ELECTION_ROUND, read from the same env so
# the two packages agree). Names the round this process operates on so R1 and R2 never collide on
# local snapshot paths / bucket keys. "r1" (default) = legacy un-prefixed paths so first-round data
# never moves; any other round (e.g. "r2") nests under a {round}/ sub-path.
ELECTION_ROUND = os.environ.get("E14_ELECTION_ROUND", "r1").strip().lower() or "r1"

# --- Host (Akamai CDN; serves the SPA, the divipol JSON, and the acta PDFs) ---
HOST = "divulgacione14presidente.registraduria.gov.co"
BASE = f"https://{HOST}"

# Static JSON data source (the app's divipol_json fallback bundles).
JSON_BASE = f"{BASE}/assets/temis/divipol_json"
JSON_FILES = {
    "transmission_codes": "allTransmissionCodes.json",   # full national acta list
    "departments_tree": "departmentsTree.json",
    "corporations": "allCorporations.json",
    "departments": "allDepartments.json",
}

# Acta PDFs live under this prefix (static files).
PDF_BASE = f"{BASE}/assets/temis/pdf"

# --- Optional GraphQL fallback (AppSync, AWS_IAM SigV4 via anon Cognito) ---
# Not used by the static-file path; kept so auth.py/api.py import + work as a
# staleness fallback. See docs/ENDPOINTS.md.
GRAPHQL_URL = "https://apx2e14awsprodpresidencia.prdtpssas.com/graphql"
AWS_REGION = "us-east-2"
APPSYNC_SERVICE = "appsync"
COGNITO_IDENTITY_POOL_ID = "us-east-2:58326cd4-70d8-4b4c-bd34-adc55fa72dc3"
COGNITO_IDENTITY_URL = f"https://cognito-identity.{AWS_REGION}.amazonaws.com/"
GQL_TIMEOUT = float(os.environ.get("E14_GQL_TIMEOUT", "120"))

# Presidential corporation. Confirmed: idCorporationCode "001" => acronym "PRE".
CORP_CODE = "001"
CORP_ACRONYM = "PRE"

# Browser-like headers are REQUIRED — Akamai resets non-browser clients, and
# the data/PDF paths additionally require the ak_bmsc/bm_sv cookies that the
# homepage sets (see session.py).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CONTACT = os.environ.get("E14_CONTACT", "electoral-transparency-research")

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
    "Referer": BASE + "/",
}

# Timeouts (the CDN is fine; JSON is ~6 MB so allow room).
JSON_TIMEOUT = float(os.environ.get("E14_JSON_TIMEOUT", "120"))
PDF_TIMEOUT = float(os.environ.get("E14_PDF_TIMEOUT", "60"))

# Defaults (overridable via CLI). The PDFs are cached static files on Akamai's
# edge, which comfortably handles high concurrency (tested: 24 workers / 40 req/s,
# 0 failures). These defaults are brisk-but-polite; raise for the full run.
DEFAULT_CONCURRENCY = int(os.environ.get("E14_CONCURRENCY", "16"))
DEFAULT_RATE_LIMIT = float(os.environ.get("E14_RATE_LIMIT", "20"))  # requests/sec


def pdf_path(dep: str, muni: str, zona: str, puesto: str, mesa: str,
             expected_name: str, acronym: str = CORP_ACRONYM) -> str:
    """Build the acta PDF path component (no host). Padding confirmed live:
    dep->2, muni->3, zona->3, puesto->2, mesa->3.
    """
    return (
        f"{dep.zfill(2)}/{muni.zfill(3)}/{zona.zfill(3)}/"
        f"{puesto.zfill(2)}/{mesa.zfill(3)}/{acronym}/{expected_name}"
    )


def pdf_url(dep: str, muni: str, zona: str, puesto: str, mesa: str,
            expected_name: str, acronym: str = CORP_ACRONYM) -> str:
    return f"{PDF_BASE}/{pdf_path(dep, muni, zona, puesto, mesa, expected_name, acronym)}"
