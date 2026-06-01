"""Download acta PDFs with atomic writes, provenance, and soft-failure checks."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config
from .manifest import Manifest
from .session import CdnSession
from .universe import ActaRecord
from .util import progress

log = logging.getLogger("e14.download")


class SoftFailure(Exception):
    """Server returned 200 but the body is not a usable acta PDF."""


def _validate_pdf(content: bytes, content_type: str) -> None:
    if not content:
        raise SoftFailure("empty body")
    if content[:5] != b"%PDF-":
        head = content[:40].decode("latin-1", "replace")
        if content.lstrip()[:9].lower() == b"<!doctype":
            raise SoftFailure("HTML fallback page (acta not present)")
        raise SoftFailure(f"not a PDF (magic={head!r}, type={content_type})")


def download_one(rec: ActaRecord, session: CdnSession, out_root: Path,
                 variant: str, force: bool = False) -> dict:
    """Fetch a single acta. Returns a provenance dict (status in result)."""
    url = rec.pdf_url()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    prov: dict = {"source_url": url, "fetched_at_utc": now,
                  "expected_name": rec.expected_name,
                  "dep": rec.dep, "muni": rec.muni, "zona": rec.zona,
                  "puesto": rec.puesto, "mesa": rec.mesa, "corp": rec.corp}
    try:
        r = session.get(url, timeout=config.PDF_TIMEOUT,
                        headers={"Accept": "application/pdf,*/*"})
    except Exception as exc:
        prov.update(status="failed", reason=f"fetch error: {exc}")
        return prov

    prov["http_status"] = r.status_code
    prov["content_type"] = r.headers.get("Content-Type")
    prov["content_length"] = int(r.headers.get("Content-Length") or 0) or None
    if r.status_code != 200:
        prov.update(status="failed", reason=f"HTTP {r.status_code}")
        return prov

    content = r.content
    try:
        _validate_pdf(content, prov["content_type"] or "")
    except SoftFailure as exc:
        prov.update(status="failed", reason=str(exc))
        return prov

    sha = hashlib.sha256(content).hexdigest()
    out_dir = out_root / rec.rel_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / rec.filename(variant)
    if dest.exists() and not force:
        prov.update(status="done", sha256=sha, byte_size=len(content),
                    server_filename=rec.expected_name, reason="already on disk")
        return prov

    # atomic write: temp -> fsync -> rename
    tmp = out_dir / (rec.filename(variant) + ".part")
    with tmp.open("wb") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(dest)

    prov.update(status="done", sha256=sha, byte_size=len(content),
                server_filename=rec.expected_name)
    return prov


def run_download(records: list[ActaRecord], manifest: Manifest, out_root: Path,
                 variant: str, concurrency: int, rate: float,
                 force: bool = False, only_failed: bool = False,
                 results_path: Path | None = None) -> dict:
    """Download records (respecting manifest resume state).

    Writes one JSON line per acta to `results_path` (append) so every success
    and failure is recorded individually for later inspection/retry.
    """
    from .util import RateLimiter

    manifest.seed(records, variant)
    out_root.mkdir(parents=True, exist_ok=True)

    if force:
        todo = records
    else:
        wanted = manifest.pending_keys(variant, only_failed=only_failed)
        todo = [r for r in records if r.key in wanted]

    skipped = len(records) - len(todo)
    log.info("download: %d to fetch, %d already done/skipped (variant=%s)",
             len(todo), skipped, variant)

    session = CdnSession(rate_limiter=RateLimiter(rate))
    if todo:
        session.prime()

    done = failed = 0
    bytes_total = 0
    reasons: Counter = Counter()
    bar = progress(range(len(todo)), total=len(todo), desc=f"E14:{variant}")
    bar_it = iter(bar)

    PROV_COLS = ("source_url", "http_status", "content_type", "content_length",
                 "sha256", "byte_size", "server_filename", "fetched_at_utc",
                 "dep", "muni", "zona", "puesto", "mesa", "corp", "expected_name")

    rlog = open(results_path, "a", encoding="utf-8") if results_path else None
    rlog_lock = threading.Lock()

    def persist(rec: ActaRecord, prov: dict):
        fields = {k: prov.get(k) for k in PROV_COLS if k in prov}
        if prov["status"] == "done":
            manifest.mark_done(rec.key, variant, **fields)
        else:
            manifest.mark_failed(rec.key, variant, prov.get("reason", ""), **fields)
        if rlog:
            line = json.dumps({
                "key": rec.key, "variant": variant, "status": prov["status"],
                "http": prov.get("http_status"), "bytes": prov.get("byte_size"),
                "sha256": prov.get("sha256"), "reason": prov.get("reason"),
                "url": prov.get("source_url"), "ts": prov.get("fetched_at_utc"),
            })
            with rlog_lock:
                rlog.write(line + "\n")

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {ex.submit(download_one, r, session, out_root, variant, force): r
                    for r in todo}
            for fut in as_completed(futs):
                rec = futs[fut]
                try:
                    prov = fut.result()
                except Exception as exc:  # defensive: never lose a worker
                    prov = {"status": "failed", "reason": f"worker crash: {exc}"}
                persist(rec, prov)
                if prov["status"] == "done":
                    done += 1
                    bytes_total += prov.get("byte_size") or 0
                else:
                    failed += 1
                    reasons[(prov.get("reason") or "unknown")[:60]] += 1
                    log.debug("FAIL %s: %s", rec.key, prov.get("reason"))
                try:
                    next(bar_it)
                except StopIteration:
                    pass
    finally:
        if rlog:
            rlog.close()

    return {"attempted": len(todo), "done": done, "failed": failed,
            "skipped": skipped, "bytes": bytes_total,
            "reasons": dict(reasons)}
