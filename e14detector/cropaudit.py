"""Crop-existence audit (P1.D).

Coverage says an acta is *served*; this says its crop actually *exists*. A served acta whose
candidate crop 404s in the object store is a broken promise to a citizen who clicks it, so
`e14 sync verify --check-crops` does a FULL sweep (not a sample) of every served candidate crop
against the bucket's real object keys and flags any orphan.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _crop_key(raw: str) -> str:
    """The ``crops/<file>`` object key for a stored crop path (mirrors webapp.crop_key, inlined
    so this stays importable without pulling in FastAPI)."""
    s = str(raw).replace("\\", "/")
    i = s.find("crops/")
    return s[i:] if i != -1 else s.lstrip("/")


def served_crop_keys(served_db: Path) -> set[str]:
    """Every candidate-crop object key referenced by the served DB."""
    con = sqlite3.connect(f"file:{Path(served_db).resolve()}?mode=ro", uri=True, timeout=60.0)
    try:
        rows = con.execute(
            "SELECT raw_crop_path FROM vote_fields "
            "WHERE row_type='candidate' AND raw_crop_path IS NOT NULL")
        return {_crop_key(r[0]) for r in rows}
    finally:
        con.close()


def audit_served_crops(output_dir: Path, *, bucket: str | None = None, client=None) -> list[str]:
    """Return a list of problem strings (empty == clean). Confirms every served candidate crop
    resolves to a real bucket object; any served key missing from the bucket is an orphan."""
    served_db = Path(output_dir) / "results" / "results.sqlite"
    if not served_db.exists():
        return ["auditoría de recortes: no hay DB servida local"]
    served = served_crop_keys(served_db)
    if not served:
        return []
    from .publish import list_bucket_crop_keys

    bucket_keys = list_bucket_crop_keys(bucket=bucket, client=client)
    orphans = sorted(served - bucket_keys)
    if not orphans:
        return []
    sample = ", ".join(orphans[:10])
    tail = " …" if len(orphans) > 10 else ""
    return [f"{len(orphans)} recorte(s) servidos sin objeto en el bucket (404): {sample}{tail}"]
