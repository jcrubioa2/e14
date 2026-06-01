"""Akamai-aware HTTP session for the static CDN data/PDF paths.

The CDN sits behind Akamai bot management which fingerprints the TLS/JA3 of the
client: plain `requests`/urllib3 connections are silently dropped (reads hang),
and the JSON/PDF asset paths require the `ak_bmsc`/`bm_sv` cookies that the
homepage sets. We therefore use `curl_cffi` impersonating Chrome (correct TLS
fingerprint + HTTP/2), prime the Akamai cookies via the homepage, and re-prime
on the tell-tale failures (connection drop / 403 challenge).

curl_cffi sessions are not safe to share across threads, so each worker thread
gets its own lazily-primed session (its own cookie jar). The rate limiter is
shared so politeness is global across all workers.
"""
from __future__ import annotations

import logging
import threading

from curl_cffi import requests as creq
from curl_cffi.requests import RequestsError

from . import config
from .util import RateLimiter, RetryableError, retry

log = logging.getLogger("e14.session")

# Chrome build whose TLS/JA3 + HTTP2 fingerprint Akamai accepts.
IMPERSONATE = "chrome"


class CdnSession:
    """Thread-safe facade: one curl_cffi session + cookie jar per thread."""

    def __init__(self, rate_limiter: RateLimiter | None = None):
        self.rate = rate_limiter or RateLimiter(config.DEFAULT_RATE_LIMIT)
        self._tl = threading.local()

    # --- per-thread session management ---

    def _session(self):
        s = getattr(self._tl, "s", None)
        if s is None:
            s = creq.Session(impersonate=IMPERSONATE)
            self._tl.s = s
            self._tl.primed = False
        return s

    def prime(self, force: bool = False) -> None:
        s = self._session()
        if getattr(self._tl, "primed", False) and not force:
            return
        try:
            r = s.get(config.BASE + "/",
                      headers={"Accept": "text/html,application/xhtml+xml"},
                      timeout=config.JSON_TIMEOUT)
        except RequestsError as exc:
            raise RetryableError(f"prime failed: {exc}") from exc
        have = [c for c in ("ak_bmsc", "bm_sv") if c in s.cookies]
        log.info("primed Akamai session (thread %s): HTTP %s cookies=%s",
                 threading.get_ident(), r.status_code, have or "none")
        self._tl.primed = True

    @retry(attempts=5, base=2.0, cap=45.0, exceptions=(RetryableError,))
    def get(self, url: str, *, timeout: float, headers: dict | None = None):
        s = self._session()
        if not getattr(self._tl, "primed", False):
            self.prime()
        self.rate.acquire()
        try:
            r = s.get(url, headers=headers, timeout=timeout)
        except RequestsError as exc:
            self._tl.primed = False
            self.prime(force=True)
            raise RetryableError(f"connection: {exc}") from exc

        if r.status_code in (401, 403):
            self._tl.primed = False
            self.prime(force=True)
            raise RetryableError(f"HTTP {r.status_code} (re-primed)")
        if r.status_code in (429, 500, 502, 503, 504):
            raise RetryableError(f"HTTP {r.status_code}")
        return r

    def get_json(self, url: str):
        r = self.get(url, timeout=config.JSON_TIMEOUT,
                     headers={"Accept": "application/json, text/plain, */*"})
        if r.status_code != 200:
            raise RetryableError(f"json HTTP {r.status_code}")
        return r.json()
