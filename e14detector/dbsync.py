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

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.request
from pathlib import Path

DB_PREFIX = "db"
POINTER_KEY = f"{DB_PREFIX}/latest.json"
# Publish lock: a small bucket object that, when present and locked, makes every
# (current-code) publisher refuse to flip the live pointer. Set it once the served DB is
# "done" (e.g. 100% of actas) so a stray publish-loop or a partial run can't overwrite it;
# only an admin toggle (or E14_DB_ALLOW_LOCKED=1) lifts it. The bucket is the shared source
# of truth, so the lock holds across machines.
LOCK_KEY = f"{DB_PREFIX}/lock.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- publisher (local) -----------------------------------------------------

def make_snapshot(src_db: Path, dest: Path) -> str:
    """Write a consistent single-file (non-WAL) snapshot of ``src_db``; return its sha256.

    ``VACUUM INTO`` takes a read transaction (safe while the crop writer is active under
    WAL) and produces a compact DELETE-journal DB — exactly what the read-only reader
    needs, and smaller than a table-by-table copy.
    """
    src_db = Path(src_db)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    con = sqlite3.connect(f"file:{src_db.resolve()}?mode=ro", uri=True, timeout=120.0)
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()
    return _sha256(dest)


# The only columns the public vote-counting site reads from vote_fields. The detector's
# working DB carries ~30 columns (CV scores, VLM json, debug/slot/comparison crop paths)
# plus a cv_features table — ~2 GB the site never queries. Serving just the candidate
# registry keeps the whole DB small enough to sit in the box's page cache, so the feed
# stays warm. ``vlm_classification`` is kept: it's a short, mostly-NULL enum still read by
# the appeal seed flag (lookup_candidate_appeal), and costs almost nothing.
_SERVE_VF_COLS = (
    "id, document_id, page_number, row_number, row_type, section, "
    "candidate_number, candidate_name, raw_crop_path, vlm_classification"
)
_SERVE_VF_DDL = """
CREATE TABLE vote_fields (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    row_number INTEGER NOT NULL,
    row_type TEXT NOT NULL,
    section TEXT,
    candidate_number INTEGER,
    candidate_name TEXT,
    raw_crop_path TEXT,
    vlm_classification TEXT
)
"""


def build_serving_db(src_db: Path, dest: Path) -> str:
    """Build the slim public-serving DB and return its sha256.

    Copies only the candidate registry (the columns above) + the full ``documents`` table
    (the /acta view does ``SELECT *``) + the indexes the live queries use. ``id`` values are
    preserved verbatim so the feed's dense random-PK sampling still works. The source is
    read read-only (``mode=ro``), so this is safe while the local crop writer runs under WAL.
    """
    src_db = Path(src_db)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    con = sqlite3.connect(dest, uri=True, timeout=120.0)
    try:
        con.execute(f"ATTACH DATABASE 'file:{src_db.resolve()}?mode=ro' AS src")
        con.executescript(_SERVE_VF_DDL)
        con.execute(
            f"INSERT INTO vote_fields ({_SERVE_VF_COLS}) "
            f"SELECT {_SERVE_VF_COLS} FROM src.vote_fields"
        )
        # documents copied whole, with its original schema (PK + columns) reproduced.
        doc_sql = con.execute(
            "SELECT sql FROM src.sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()[0]
        con.execute(doc_sql)
        con.execute("INSERT INTO documents SELECT * FROM src.documents")
        # Precompute candidate-crop count per acta so /browse never has to join+GROUP BY the
        # 1.5M-row vote_fields table on every page load (that was ~4s). With this column the
        # browse list is a pure documents-table query (~100k rows). Newer source DBs already
        # carry n_candidates (maintained by DetectorStore); older ones don't — add it if
        # missing, then recompute here so the served value is always correct and authoritative.
        if "n_candidates" not in {r[1] for r in con.execute("PRAGMA table_info(documents)")}:
            con.execute("ALTER TABLE documents ADD COLUMN n_candidates INTEGER NOT NULL DEFAULT 0")
        # Build the vote_fields indexes FIRST: the n_candidates recompute below is a correlated
        # COUNT subquery keyed on (document_id, row_type), so without this index it degrades to a
        # full vote_fields scan per document (~113k × 1.5M rows = minutes). With it, each count is
        # an index range probe and the whole UPDATE runs in seconds.
        con.execute("CREATE INDEX idx_vf_doc_type ON vote_fields(document_id, row_type)")
        con.execute("CREATE INDEX idx_vf_crop ON vote_fields(raw_crop_path)")
        con.execute(
            "UPDATE documents SET n_candidates = COALESCE("
            "(SELECT COUNT(*) FROM vote_fields vf WHERE vf.document_id = documents.document_id "
            "AND vf.row_type='candidate' AND vf.raw_crop_path IS NOT NULL), 0)"
        )
        # Remaining indexes the live site relies on: geo drill-down and the browse list order
        # (only over actas that have candidate crops).
        doc_cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        if "department_code" in doc_cols:
            con.execute("CREATE INDEX idx_doc_browse ON documents(department_code, document_id) WHERE n_candidates>0")
        if {"department_code", "municipality_code", "zone", "puesto"} <= doc_cols:
            con.execute("CREATE INDEX idx_doc_geo ON documents(department_code, municipality_code, zone, puesto)")
        con.commit()
    finally:
        con.close()
    return _sha256(dest)


def _s3_client():
    import boto3  # local-only; lazy so the reader/serve path never imports it

    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    return boto3.client("s3", endpoint_url=endpoint)


def _prune_to_uploaded(work_db: Path, uploaded: set[str]) -> tuple[int, int]:
    """Drop documents whose candidate crops aren't all uploaded yet (the safe frontier).

    Returns (kept_documents, dropped_documents). Lets the publisher run continuously
    alongside the crop run without ever showing an acta whose crop 404s.
    """
    from collections import defaultdict

    from .webapp import crop_key  # local-only path; lazy so the reader never imports webapp

    con = sqlite3.connect(work_db)
    con.row_factory = sqlite3.Row
    try:
        by_doc: dict[str, list[str]] = defaultdict(list)
        for r in con.execute(
            "SELECT document_id, raw_crop_path FROM vote_fields "
            "WHERE row_type='candidate' AND raw_crop_path IS NOT NULL"
        ):
            by_doc[r["document_id"]].append(crop_key(r["raw_crop_path"]))
        incomplete = [d for d, keys in by_doc.items() if any(k not in uploaded for k in keys)]
        kept = len(by_doc) - len(incomplete)
        if incomplete:
            con.execute("CREATE TEMP TABLE _drop(id TEXT PRIMARY KEY)")
            con.executemany("INSERT INTO _drop VALUES (?)", [(d,) for d in incomplete])
            con.execute("DELETE FROM vote_fields WHERE document_id IN (SELECT id FROM _drop)")
            con.execute("DELETE FROM documents WHERE document_id IN (SELECT id FROM _drop)")
            con.commit()
            con.execute("VACUUM")
        return kept, len(incomplete)
    finally:
        con.close()


def _resolve_bucket(bucket: str | None) -> str | None:
    return bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")


def read_db_lock(*, client=None, bucket: str | None = None) -> dict:
    """Return the publish lock object (``{"locked": bool, ...}``). Best-effort: a missing
    object or any read error reads as unlocked so a flaky bucket never blocks publishing."""
    bucket = _resolve_bucket(bucket)
    if not bucket:
        return {"locked": False}
    if client is None:
        client = _s3_client()
    try:
        d = json.loads(client.get_object(Bucket=bucket, Key=LOCK_KEY)["Body"].read())
        return d if isinstance(d, dict) else {"locked": False}
    except Exception:
        return {"locked": False}


def set_db_lock(locked: bool, *, reason: str = "", n_docs: int | None = None, by: str = "",
                client=None, bucket: str | None = None) -> dict:
    """Write the publish lock object. ``locked=False`` records an explicit unlock (kept, not
    deleted, so the admin board can show who/when). Returns the stored body."""
    bucket = _resolve_bucket(bucket)
    if not bucket:
        raise ValueError("no bucket: set BUCKET_NAME or pass bucket")
    if client is None:
        client = _s3_client()
    body = {"locked": bool(locked), "reason": reason, "n_docs": n_docs,
            "by": by, "ts": int(time.time())}
    client.put_object(
        Bucket=bucket, Key=LOCK_KEY, Body=json.dumps(body).encode(),
        ContentType="application/json", CacheControl="no-store, max-age=0",
    )
    return body


def publish_db(
    output_dir: Path,
    *,
    bucket: str | None = None,
    client=None,
    only_uploaded: bool = False,
    manifest: Path | None = None,
    allow_shrink: bool = False,
    allow_locked: bool = False,
    verbose: bool = True,
) -> dict | None:
    """Snapshot the local results DB and publish it + the pointer to the bucket.

    With ``only_uploaded``, the snapshot is pruned to the *frontier*: only actas whose
    candidate crops are all in the upload manifest. Returns None if that frontier is empty
    (nothing safe to publish yet) so the loop can simply wait.

    Guard: refuses to flip the live pointer to a DB that holds drastically fewer actas
    (<50% of the live ``n_docs``) than the one currently published, unless ``allow_shrink``.
    This prevents a misconfigured run (e.g. wrong --output-dir pointing at a stub DB) from
    nuking the live national DB. The count — not raw bytes — is the real "did we lose actas"
    signal: a schema slim-down legitimately halves the bytes while keeping every acta, so a
    byte-size guard would false-trip on it. Legacy pointers written before ``n_docs`` existed
    fall back to the old byte heuristic (which fires at most once: the gated publish writes
    ``n_docs``, after which the guard is permanently count-based).

    Lock: if the bucket carries a locked ``db/lock.json`` (see ``set_db_lock``), publishing is
    refused unless ``allow_locked``. Used to freeze the served DB once it is complete.
    """
    src = Path(output_dir) / "results" / "results.sqlite"
    if not src.exists():
        raise FileNotFoundError(f"results DB not found: {src}")
    bucket = bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if os.environ.get("E14_DB_MERGE_BEFORE_PUBLISH", "1").lower() not in ("0", "false", "no"):
        try:
            pull_db(output_dir, bucket=bucket, verbose=verbose)
        except Exception as exc:
            if verbose:
                print(f"publish-db: pull/merge skipped ({type(exc).__name__}: {exc})", flush=True)
    if not bucket:
        raise ValueError("no bucket: set BUCKET_NAME or pass --bucket")
    if client is None:
        client = _s3_client()

    # Publish lock: once the served DB is marked done, refuse to overwrite it. Checked before
    # the (expensive) slim build so a locked publisher exits fast. Admin toggle / allow_locked
    # / E14_DB_ALLOW_LOCKED=1 override.
    if not allow_locked and os.environ.get("E14_DB_ALLOW_LOCKED", "").lower() not in ("1", "true", "yes"):
        lock = read_db_lock(client=client, bucket=bucket)
        if lock.get("locked"):
            msg = (f"publish-db: REFUSING — the live DB is LOCKED"
                   f"{' (' + lock['reason'] + ')' if lock.get('reason') else ''}. "
                   f"Unlock from the admin console (or pass allow_locked / E14_DB_ALLOW_LOCKED=1).")
            if verbose:
                print(msg, flush=True)
            return {"locked": True, "lock": lock}

    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "snapshot.sqlite"
        # Slim public-serving snapshot (registry + geo only), not a full copy of the
        # detector working DB — see build_serving_db.
        build_serving_db(src, snap)
        if only_uploaded:
            manifest = Path(manifest) if manifest else Path(output_dir) / "review" / "uploaded_crops.txt"
            uploaded = set(manifest.read_text(encoding="utf-8").split()) if manifest.exists() else set()
            kept, dropped = _prune_to_uploaded(snap, uploaded)
            if verbose:
                print(f"publish-db: frontier = {kept} fully-uploaded acta(s), {dropped} held back", flush=True)
            if kept == 0:
                return None  # nothing safe to publish yet
        digest = _sha256(snap)  # sha of the DECOMPRESSED db (what the reader installs)
        raw_size = snap.stat().st_size
        # Acta count is the guard's real signal (invariant to schema slimming). In
        # --only-uploaded mode this is the kept frontier; otherwise the whole registry.
        _c = sqlite3.connect(snap)
        try:
            n_docs = _c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            _c.close()
        key = f"{DB_PREFIX}/results-{digest[:16]}.sqlite.gz"
        # Inspect the currently-published pointer for the unchanged-skip and shrink guard.
        try:
            cur = json.loads(client.get_object(Bucket=bucket, Key=POINTER_KEY)["Body"].read())
        except Exception:
            cur = None  # no pointer yet, or client without get_object — just publish
        if cur is not None:
            if cur.get("sha256") == digest:
                if verbose:
                    print("publish-db: unchanged since last publish; skipping upload", flush=True)
                return {"key": key, "sha256": digest, "size": cur.get("size", 0),
                        "n_docs": n_docs, "kept": kept if only_uploaded else None, "skipped": True}
            cur_docs = cur.get("n_docs")
            cur_raw = cur.get("raw_size", 0)
            guard_msg = None
            if not allow_shrink:
                if cur_docs:
                    # Count-based guard: refuse only on a real drop in actas. Bytes ignored,
                    # so a slimmer-but-complete snapshot publishes normally.
                    if n_docs < 0.5 * cur_docs:
                        guard_msg = (f"publish-db: REFUSING to publish — new DB has {n_docs} acta(s), "
                                     f"<50% of the live DB's {cur_docs}. Wrong --output-dir? "
                                     f"Pass allow_shrink=True to override.")
                elif cur_raw and raw_size < 0.5 * cur_raw:
                    # Legacy pointer without n_docs (one-time migration): fall back to bytes.
                    # A slim-down can legitimately trip this once; verify the acta count, then
                    # override with --allow-shrink. The resulting publish records n_docs, so
                    # subsequent runs are guarded on count and never false-trip on size again.
                    guard_msg = (f"publish-db: REFUSING to publish — new DB ({raw_size/1e6:.0f} MB) is "
                                 f"<50% of the live DB ({cur_raw/1e6:.0f} MB) and the live pointer "
                                 f"predates acta-count tracking. If this is a slim-down (acta count "
                                 f"OK), pass allow_shrink=True once to override.")
            if guard_msg:
                if verbose:
                    print(guard_msg, flush=True)
                return {"key": key, "sha256": digest, "size": 0, "raw_size": raw_size,
                        "n_docs": n_docs, "kept": kept if only_uploaded else None, "guarded": True}
        # gzip the snapshot — a paths/metadata DB (mostly NULL columns + repetitive crop
        # paths) compresses ~10x, so the upload is far smaller and cycles stay short.
        gz = Path(str(snap) + ".gz")
        # Level 1: ~3-4x faster than 6 for a small size penalty — the snapshot grows with
        # the rollout, so keep per-cycle compression cheap (the reader auto-detects level).
        with open(snap, "rb") as f_in, gzip.open(gz, "wb", compresslevel=1) as f_out:
            shutil.copyfileobj(f_in, f_out, 1 << 20)
        gz_size = gz.stat().st_size
        if verbose:
            print(f"publish-db: {raw_size/1e6:.0f} MB -> {gz_size/1e6:.1f} MB gz "
                  f"sha={digest[:12]} -> {bucket}/{key}", flush=True)
        client.upload_file(  # immutable content-addressed object (cache forever)
            str(gz), bucket, key,
            ExtraArgs={"ContentType": "application/gzip",
                       "CacheControl": "public, max-age=31536000, immutable"},
        )
        # ... then flip the pointer last (never cache it).
        pointer = json.dumps({"key": key, "sha256": digest, "size": gz_size,
                              "raw_size": raw_size, "n_docs": n_docs, "ts": int(time.time())})
        client.put_object(
            Bucket=bucket, Key=POINTER_KEY, Body=pointer.encode(),
            ContentType="application/json", CacheControl="no-store, max-age=0",
        )
    return {"key": key, "sha256": digest, "size": gz_size, "n_docs": n_docs,
            "kept": kept if only_uploaded else None}


# --- multi-writer merge (local crop machines) --------------------------------

def _vote_field_columns(con: sqlite3.Connection, *, table_alias: str = "") -> str:
    """Comma-separated vote_fields columns for INSERT…SELECT (excludes AUTOINCREMENT id)."""
    if table_alias:
        pragma = f"PRAGMA {table_alias}.table_info(vote_fields)"
    else:
        pragma = "PRAGMA table_info(vote_fields)"
    cols = [r[1] for r in con.execute(pragma) if r[1] != "id"]
    if table_alias:
        return ", ".join(f"{table_alias}.{c}" for c in cols)
    return ", ".join(cols)


def merge_results_db(local_db: Path, remote_db: Path, *, verbose: bool = True) -> dict:
    """Merge a published remote snapshot into the local writer DB.

    Coordination rule: **local wins** on document_id conflicts (this machine's in-progress
    work is kept). Rows from remote are copied only for actas this machine has never
    recorded — so after ``pull-db`` every PC skips actas another PC already finished.

    Merges ``documents`` and ``vote_fields`` (enough for crop resume + publish frontier).
    """
    local_db = Path(local_db)
    remote_db = Path(remote_db)
    if not remote_db.exists():
        raise FileNotFoundError(remote_db)
    con = sqlite3.connect(local_db, timeout=120.0)
    con.execute("PRAGMA busy_timeout=30000")
    try:
        docs_before = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        con.execute("ATTACH DATABASE ? AS remote", (str(remote_db.resolve()),))
        pending = {
            r[0]
            for r in con.execute(
                "SELECT document_id FROM remote.documents "
                "WHERE document_id NOT IN (SELECT document_id FROM main.documents)"
            )
        }
        con.execute("INSERT OR IGNORE INTO main.documents SELECT * FROM remote.documents")
        docs_added = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] - docs_before
        main_cols = _vote_field_columns(con)
        # Unqualified SELECT cols: SQLite attached DB rejects ``remote.col`` in SELECT
        # (but unqualified names resolve against remote.vote_fields).
        select_cols = _vote_field_columns(con)
        fields_added = 0
        if pending and select_cols:
            before_vf = con.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0]
            placeholders = ", ".join("?" for _ in pending)
            con.execute(
                f"INSERT INTO main.vote_fields ({main_cols}) "
                f"SELECT {select_cols} FROM remote.vote_fields "
                f"WHERE document_id IN ({placeholders})",
                tuple(pending),
            )
            fields_added = con.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0] - before_vf
        con.commit()
        docs_total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if verbose:
            print(
                f"merge-db: +{docs_added} actas, +{fields_added} vote_fields "
                f"({docs_total:,} total documents)",
                flush=True,
            )
        return {
            "docs_added": docs_added,
            "fields_added": fields_added,
            "docs_total": docs_total,
            "remote_pending": len(pending),
        }
    finally:
        try:
            con.execute("DETACH DATABASE remote")
        except sqlite3.Error:
            pass
        con.close()


def _download_snapshot_file(
    pointer: dict,
    dest: Path,
    *,
    cdn_base: str | None,
    bucket: str | None,
    client,
    timeout: float,
) -> None:
    key = pointer["key"]
    want = pointer["sha256"]
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if cdn_base:
        with urllib.request.urlopen(
            urllib.request.Request(f"{cdn_base.rstrip('/')}/{key}"), timeout=timeout
        ) as resp:
            stream = gzip.GzipFile(fileobj=resp) if key.endswith(".gz") else resp
            with open(dest, "wb") as out:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    out.write(chunk)
    else:
        if client is None:
            client = _s3_client()
        if not bucket:
            raise ValueError("no bucket for pull-db")
        obj = client.get_object(Bucket=bucket, Key=key)
        body = obj["Body"]
        if key.endswith(".gz"):
            with gzip.GzipFile(fileobj=body) as gz, open(dest, "wb") as out:
                shutil.copyfileobj(gz, out, 1 << 20)
        else:
            with open(dest, "wb") as out:
                shutil.copyfileobj(body, out, 1 << 20)
    got = _sha256(dest)
    if got != want:
        raise ValueError(f"snapshot sha mismatch: got {got[:12]} want {want[:12]}")


def fetch_published_pointer(
    *,
    cdn_base: str | None = None,
    bucket: str | None = None,
    client=None,
    timeout: float = 30.0,
) -> dict | None:
    """Return the live ``db/latest.json`` object, or None if unpublished / unreachable."""
    if cdn_base:
        sep = "&" if "?" in POINTER_KEY else "?"
        url = f"{cdn_base.rstrip('/')}/{POINTER_KEY}"
        if url.startswith("http"):
            url += f"{sep}t={int(time.time())}"
        try:
            return json.loads(_fetch(url, timeout))
        except Exception:
            return None
    bucket = bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if not bucket:
        return None
    if client is None:
        client = _s3_client()
    try:
        return json.loads(client.get_object(Bucket=bucket, Key=POINTER_KEY)["Body"].read())
    except Exception:
        return None


def pull_db(
    output_dir: Path,
    *,
    cdn_base: str | None = None,
    bucket: str | None = None,
    client=None,
    timeout: float = 120.0,
    verbose: bool = True,
) -> dict | None:
    """Download the live published DB and merge it into the local results DB."""
    local = Path(output_dir) / "results" / "results.sqlite"
    local.parent.mkdir(parents=True, exist_ok=True)
    if not local.exists():
        from .storage import DetectorStore

        DetectorStore(local, None).close()
    cdn_base = cdn_base or os.environ.get("E14_CDN_BASE_URL") or ""
    pointer = fetch_published_pointer(cdn_base=cdn_base or None, bucket=bucket, client=client, timeout=timeout)
    if pointer is None:
        if verbose:
            print("pull-db: no published pointer (nothing to merge yet)", flush=True)
        return None
    with tempfile.TemporaryDirectory() as td:
        remote = Path(td) / "remote.sqlite"
        _download_snapshot_file(
            pointer, remote, cdn_base=cdn_base or None, bucket=bucket, client=client, timeout=timeout
        )
        stats = merge_results_db(local, remote, verbose=verbose)
    stats["sha256"] = pointer.get("sha256", "")[:12]
    return stats


# --- pointer status (reader-side, stdlib) ----------------------------------

def pointer_status(cdn_base: str, *, timeout: float = 10.0) -> dict | None:
    """Fetch the published pointer and return freshness/size info for operator dashboards.

    Stdlib-only (no boto3), safe to call from the serve image. Returns None on any failure
    so a flaky fetch never breaks the admin page.
    """
    try:
        base = cdn_base.rstrip("/")
        sep = "&" if "?" in POINTER_KEY else "?"
        url = f"{base}/{POINTER_KEY}"
        if base.startswith("http"):
            url += f"{sep}t={int(time.time())}"
        p = json.loads(_fetch(url, timeout))
        ts = int(p.get("ts", 0))
        return {
            "sha": (p.get("sha256") or "")[:12],
            "gz_size": p.get("size", 0),
            "raw_size": p.get("raw_size", 0),
            "n_docs": p.get("n_docs", 0),
            "ts": ts,
            "age_secs": int(time.time()) - ts if ts else None,
        }
    except Exception:
        return None


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
        with urllib.request.urlopen(urllib.request.Request(f"{base}/{key}"), timeout=timeout) as resp:
            # gzip-compressed snapshots are decompressed on the fly; raw .sqlite still works.
            stream = gzip.GzipFile(fileobj=resp) if key.endswith(".gz") else resp
            with open(tmp, "wb") as out:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    out.write(chunk)
        got = _sha256(tmp)  # verify the DECOMPRESSED db against the pointer
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
