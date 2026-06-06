"""Crop-existence audit (P1.D).

Coverage says an acta is *served*; this says its crop actually *exists*. A served acta whose
candidate crop 404s in the object store is a broken promise to a citizen who clicks it, so
`e14 sync verify --check-crops` does a FULL sweep (not a sample) of every served candidate crop
against the bucket's real object keys and flags any orphan.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
from pathlib import Path


def _crop_prefix(round: str | None = None) -> str:
    """Round crop prefix (mirrors webapp.crop_prefix, inlined so this stays importable without
    pulling in FastAPI). ``crops/`` for r1 (legacy), ``crops/<round>/`` otherwise."""
    from . import config

    r = (round or config.ELECTION_ROUND or "r1").strip().lower()
    return "crops/" if r == "r1" else f"crops/{r}/"


def _crop_key(raw: str, round: str | None = None) -> str:
    """The round-scoped object key for a stored crop path (mirrors webapp.crop_key, inlined so
    this stays importable without pulling in FastAPI). The file part is the OPAQUE HMAC of the
    crop_rel — it MUST byte-for-byte match webapp.crop_obj_name (same secret, sha256, 24-hex,
    ``.png``) or this audit will false-flag every served crop as an orphan."""
    from . import config

    s = str(raw).replace("\\", "/")
    i = s.find("crops/")
    rel = s[i + len("crops/"):] if i != -1 else s.lstrip("/")
    digest = hmac.new(config.CROP_KEY_SECRET.encode("utf-8"), rel.encode("utf-8"), hashlib.sha256)
    return _crop_prefix(round) + digest.hexdigest()[:24] + ".png"


def served_crop_keys(served_db: Path, round: str | None = None) -> set[str]:
    """Every candidate-crop object key referenced by the served DB, scoped to ``round``."""
    con = sqlite3.connect(f"file:{Path(served_db).resolve()}?mode=ro", uri=True, timeout=60.0)
    try:
        rows = con.execute(
            "SELECT raw_crop_path FROM vote_fields "
            "WHERE row_type='candidate' AND raw_crop_path IS NOT NULL")
        return {_crop_key(r[0], round) for r in rows}
    finally:
        con.close()


def audit_served_crops(output_dir: Path, *, bucket: str | None = None, client=None,
                       round: str | None = None) -> list[str]:
    """Return a list of problem strings (empty == clean). Confirms every served candidate crop
    resolves to a real bucket object; any served key missing from the bucket is an orphan."""
    served_db = Path(output_dir) / "results" / "results.sqlite"
    if not served_db.exists():
        return ["auditoría de recortes: no hay DB servida local"]
    served = served_crop_keys(served_db, round)
    if not served:
        return []
    from .publish import list_bucket_crop_keys

    bucket_keys = list_bucket_crop_keys(bucket=bucket, client=client, round=round)
    orphans = sorted(served - bucket_keys)
    if not orphans:
        return []
    sample = ", ".join(orphans[:10])
    tail = " …" if len(orphans) > 10 else ""
    return [f"{len(orphans)} recorte(s) servidos sin objeto en el bucket (404): {sample}{tail}"]
