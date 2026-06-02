"""Bulletproof local->Fly results-DB sync through the public object store.

Topology: the *writer* is local (the crop run + seeding build the national
``results.sqlite``); the *reader* is the Fly app, which serves it read-only and reopens
it per request. We bridge them through Tigris:

- **Publisher (local, boto3):** ``VACUUM INTO`` a consistent single-file snapshot, upload
  it under a content-hashed, immutable key, then write a small ``db/latest.json`` pointer
  *last*. Content hashing makes re-publishing a no-op when nothing changed.
- **Reader (Fly app, stdlib only):** poll the pointer; when it names a new hash, download
  that snapshot, verify its sha256, and ``os.replace()`` it into the served path. The swap
  is atomic, so readers never observe a torn or partial DB; a failed step simply retries.

The reader path imports no third-party deps (boto3 stays out of the lean serve image);
only the local publisher uses boto3, imported lazily.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import urllib.request
from pathlib import Path

DB_PREFIX = "db"
POINTER_KEY = f"{DB_PREFIX}/latest.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- publisher (local) -----------------------------------------------------

def make_snapshot(src_db: Path, dest: Path) -> str:
    """Write a consistent single-file (non-WAL) snapshot of ``src_db``; return its sha256.

    ``VACUUM INTO`` takes a read transaction, so it is safe to run while the crop/seed
    writer is active (WAL lets readers proceed), and the output is a clean DELETE-journal
    DB — exactly what the read-only reader needs (no -wal/-shm dependency).
    """
    src_db = Path(src_db)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    con = sqlite3.connect(f"file:{src_db.resolve()}?mode=ro", uri=True, timeout=60.0)
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()
    return _sha256(dest)


def _s3_client():
    import boto3  # local-only; lazy so the reader/serve path never imports it

    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    return boto3.client("s3", endpoint_url=endpoint)


def publish_db(
    output_dir: Path,
    *,
    bucket: str | None = None,
    client=None,
    verbose: bool = True,
) -> dict:
    """Snapshot the local results DB and publish it + the pointer to the bucket."""
    src = Path(output_dir) / "results" / "results.sqlite"
    if not src.exists():
        raise FileNotFoundError(f"results DB not found: {src}")
    bucket = bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if not bucket:
        raise ValueError("no bucket: set BUCKET_NAME or pass --bucket")
    if client is None:
        client = _s3_client()

    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "snapshot.sqlite"
        digest = make_snapshot(src, snap)
        size = snap.stat().st_size
        key = f"{DB_PREFIX}/results-{digest[:16]}.sqlite"
        if verbose:
            print(f"publish-db: snapshot {size/1e6:.1f} MB sha={digest[:12]} -> {bucket}/{key}", flush=True)
        # Immutable content-addressed object (safe to cache forever) ...
        client.upload_file(
            str(snap), bucket, key,
            ExtraArgs={"ContentType": "application/x-sqlite3",
                       "CacheControl": "public, max-age=31536000, immutable"},
        )
        # ... then flip the pointer last (never cache it).
        pointer = json.dumps({"key": key, "sha256": digest, "size": size, "ts": int(time.time())})
        client.put_object(
            Bucket=bucket, Key=POINTER_KEY, Body=pointer.encode(),
            ContentType="application/json", CacheControl="no-store, max-age=0",
        )
    return {"key": key, "sha256": digest, "size": size}


# --- reader (Fly app) ------------------------------------------------------

def _marker(dest_db: Path) -> Path:
    return Path(str(dest_db) + ".sha")


def _fetch(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def refresh_db_once(cdn_base: str, dest_db: Path, *, timeout: float = 60.0) -> str | None:
    """Pull a newer snapshot if the pointer changed; atomic-swap it in.

    Returns the new sha256 if it installed one, else None. Raises only on unexpected I/O
    so the caller can log; partial work is cleaned up and never touches the served file.
    """
    base = cdn_base.rstrip("/")
    dest_db = Path(dest_db)
    sep = "&" if "?" in POINTER_KEY else "?"
    pointer_url = f"{base}/{POINTER_KEY}"
    if base.startswith("http"):
        pointer_url += f"{sep}t={int(time.time())}"  # cache-bust the pointer (http only)
    pointer = json.loads(_fetch(pointer_url, timeout))
    want, key = pointer["sha256"], pointer["key"]

    marker = _marker(dest_db)
    if dest_db.exists() and marker.exists() and marker.read_text().strip() == want:
        return None  # already serving this snapshot

    dest_db.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest_db.parent), suffix=".incoming")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{base}/{key}"), timeout=timeout) as resp, \
                open(tmp, "wb") as out:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                out.write(chunk)
        got = _sha256(tmp)
        if got != want:
            raise ValueError(f"snapshot sha mismatch: got {got[:12]} want {want[:12]}")
        # A stale -wal/-shm beside the new main file would corrupt reads; drop them.
        for ext in ("-wal", "-shm"):
            stale = Path(str(dest_db) + ext)
            if stale.exists():
                stale.unlink()
        os.replace(tmp, dest_db)  # atomic on the same filesystem
        marker.write_text(want)
        return want
    finally:
        if tmp.exists():
            tmp.unlink()
