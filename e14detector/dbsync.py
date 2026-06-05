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


# --- count-model reconciliation ---------------------------------------------
# The publisher is the only component that can see every source of truth at once (the
# external universe snapshot, the download manifest, the uploaded-crops frontier, and the
# served snapshot it is about to ship), so it stamps the whole count chain into the pointer.
# The app/admin/public page then render ONE reconciliation record instead of re-deriving
# numbers from scattered queries at different times (which is how we "went in circles").

def _served_keys(snap_db: Path) -> set[str]:
    """Mesa keys present in a served snapshot, in the canonical ``ActaRecord.key`` form.

    ``dep_muni_zona_puesto_mesa`` with the same zero-padding the universe uses, so the set
    diffs cleanly against the snapshot's informed-key list.
    """
    con = sqlite3.connect(f"file:{Path(snap_db).resolve()}?mode=ro", uri=True, timeout=60.0)
    try:
        rows = con.execute(
            "SELECT department_code, municipality_code, zone, puesto, mesa FROM documents"
        ).fetchall()
    finally:
        con.close()
    keys: set[str] = set()
    for dep, muni, zona, puesto, mesa in rows:
        if dep is None or mesa is None:
            continue
        keys.add(
            f"{str(dep).strip().zfill(2)}_{str(muni or '').strip().zfill(3)}_"
            f"{str(zona or '').strip().zfill(3)}_{str(puesto or '').strip().zfill(2)}_"
            f"{str(mesa).strip().zfill(3)}"
        )
    return keys


def _manifest_done_count(manifest_db: Path | None) -> int | None:
    """Best-effort count of downloaded actas (manifest ``status='done'``); None if absent."""
    if not manifest_db:
        manifest_db = Path("data") / "manifest.db"
    manifest_db = Path(manifest_db)
    if not manifest_db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{manifest_db.resolve()}?mode=ro", uri=True, timeout=30.0)
        try:
            return int(con.execute(
                "SELECT COUNT(DISTINCT key) FROM actas WHERE status='done'").fetchone()[0])
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — a missing/locked manifest just means "unknown", not failure
        return None


def compute_reconciliation(
    snap_db: Path,
    *,
    n_docs: int,
    output_dir: Path,
    kept: int | None = None,
    universe_path: Path | None = None,
    manifest_db: Path | None = None,
    missing_sample: int = 50,
) -> dict:
    """Build the count-model reconciliation block stamped into the pointer.

    Every field is best-effort: a source that isn't reachable from the publishing machine
    is simply omitted (rendered as "—"), never an error. The two anchors that matter most —
    the external ``total_global``/``mesas_informadas`` and the served ``sqlite_served`` — plus
    the derived backlogs are present whenever the universe snapshot exists. ``backlog_ingesta``
    is authoritative arithmetic (informadas − served); ``missing_keys_sample``/``missing_count``
    are a best-effort *enumeration* of which informed mesas aren't served yet.
    """
    rec: dict = {"sqlite_served": int(n_docs), "ts": int(time.time())}
    # External truth (the count model's E1/E2) from the universe snapshot.
    snap = None
    try:
        from e14.universe import SNAPSHOT_PATH, load_universe_snapshot

        snap = load_universe_snapshot(universe_path or SNAPSHOT_PATH)
    except Exception:  # noqa: BLE001 — snapshot optional; absence just leaves the rows blank
        snap = None
    if snap:
        tg = snap.get("total_global")
        inf = snap.get("mesas_informadas")
        rec["total_global"] = tg
        rec["mesas_escrutadas"] = snap.get("mesas_escrutadas")
        rec["mesas_informadas"] = inf
        rec["universe_fetched_at"] = snap.get("fetched_at")
        if isinstance(inf, int):
            rec["backlog_ingesta"] = max(0, inf - int(n_docs))
        if isinstance(tg, int) and isinstance(inf, int):
            rec["backlog_reporte"] = max(0, tg - inf)
        try:
            informed = set(snap.get("keys") or [])
            if informed:
                missing = sorted(informed - _served_keys(snap_db))
                rec["missing_count"] = len(missing)
                rec["missing_keys_sample"] = missing[:missing_sample]
        except Exception:  # noqa: BLE001 — enumeration is a diagnostic; arithmetic stands alone
            pass
    # Internal frontier (I1/I2). Best-effort; only present when the local files are here.
    # Only trust the manifest's downloaded count when it's >= served: you cannot serve an acta
    # you didn't download, so a lower reading means a stale/partial manifest on THIS machine, not
    # fewer real downloads — omit it (unknown) rather than stamp a phantom chain inversion.
    dl = _manifest_done_count(manifest_db)
    if dl is not None and dl >= int(n_docs):
        rec["downloaded"] = dl
    # crops_uploaded as an *acta* count is the published frontier itself: we never serve an
    # acta whose crops aren't all uploaded, so for any published snapshot it equals the kept
    # frontier (only_uploaded mode) and otherwise sqlite_served. Recording it keeps the chain
    # explicit even though I2==I3==I4 holds by construction.
    rec["crops_uploaded"] = int(kept) if kept is not None else int(n_docs)
    # Content-integrity axis (parallel to coverage, NOT part of the monotone chain): the latest
    # content report's summary, if one exists locally. Best-effort; absent on a publisher with no
    # report. Surfaced as its own line on the admin board / transparencia.
    try:
        from .contentcheck import latest_content_summary

        content = latest_content_summary()
        if content:
            rec["content"] = content
    except Exception:  # noqa: BLE001 — content axis is informational; never blocks a publish
        pass
    return rec


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
    force_pointer: bool = False,
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

    ``force_pointer``: normally an unchanged DB (same sha as the live pointer) is a no-op. With
    ``force_pointer`` the pointer is re-stamped with a fresh reconciliation block even when the
    snapshot bytes are identical — used for the one-off refresh that brings a frozen/locked
    round's pointer up to the current count model without re-uploading the snapshot.
    """
    src = Path(output_dir) / "results" / "results.sqlite"
    if not src.exists():
        raise FileNotFoundError(f"results DB not found: {src}")
    bucket = bucket or os.environ.get("BUCKET_NAME") or os.environ.get("E14_TIGRIS_BUCKET")
    if not bucket:
        raise ValueError("no bucket: set BUCKET_NAME or pass --bucket")
    if client is None:
        client = _s3_client()

    # Publish lock: once the served DB is marked done, refuse to overwrite it. Checked first
    # (before any pull/merge or the expensive slim build) so a locked publisher exits fast.
    # Admin toggle / allow_locked / E14_DB_ALLOW_LOCKED=1 override.
    if not allow_locked and os.environ.get("E14_DB_ALLOW_LOCKED", "").lower() not in ("1", "true", "yes"):
        lock = read_db_lock(client=client, bucket=bucket)
        if lock.get("locked"):
            msg = (f"publish-db: REFUSING — the live DB is LOCKED"
                   f"{' (' + lock['reason'] + ')' if lock.get('reason') else ''}. "
                   f"Unlock from the admin console (or pass allow_locked / E14_DB_ALLOW_LOCKED=1).")
            if verbose:
                print(msg, flush=True)
            return {"locked": True, "lock": lock}

    # Auto-merge before publish is OPT-IN (default OFF). Explicit `pull-db` /
    # `fleet-schedule --pull-db` own merging; a silently-skipped auto-merge is what let a
    # partial DB get published, so when it IS enabled a failure aborts the publish (no
    # swallowing) rather than shipping a possibly-incomplete snapshot.
    if os.environ.get("E14_DB_MERGE_BEFORE_PUBLISH", "").lower() in ("1", "true", "yes"):
        pull_db(output_dir, bucket=bucket, client=client, verbose=verbose)

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
            # Browsable actas (>=1 candidate crop) — what /browse and the public actually serve.
            # Shipped alongside n_docs so the admin board can reconcile "servidas" vs "publicadas"
            # from one source and never display two unexplained totals (the n_docs-vs-browsable gap
            # is exactly the documents with n_candidates=0: metadata-only / failed-crop rows).
            n_browsable = _c.execute(
                "SELECT COUNT(*) FROM documents WHERE n_candidates>0").fetchone()[0]
        finally:
            _c.close()
        # Stamp the whole count chain (best-effort; see compute_reconciliation). Computed once
        # here so every return path — published, re-stamped, skipped — carries the same record.
        recon = compute_reconciliation(
            snap, n_docs=n_docs, output_dir=Path(output_dir),
            kept=(kept if only_uploaded else None), manifest_db=None,
        )
        key = f"{DB_PREFIX}/results-{digest[:16]}.sqlite.gz"
        # Inspect the currently-published pointer for the unchanged-skip and shrink guard.
        try:
            cur = json.loads(client.get_object(Bucket=bucket, Key=POINTER_KEY)["Body"].read())
        except Exception:
            cur = None  # no pointer yet, or client without get_object — just publish
        if cur is not None:
            if cur.get("sha256") == digest:
                if not force_pointer:
                    if verbose:
                        print("publish-db: unchanged since last publish; skipping upload", flush=True)
                    return {"key": key, "sha256": digest, "size": cur.get("size", 0),
                            "n_docs": n_docs, "n_browsable": n_browsable, "reconciliation": recon,
                            "kept": kept if only_uploaded else None, "skipped": True}
                # force_pointer: the snapshot object already exists (same sha), so re-stamp ONLY
                # the pointer with a refreshed reconciliation block — no re-upload of the gz.
                pointer = json.dumps({
                    "key": cur.get("key", key), "sha256": digest,
                    "size": cur.get("size", 0), "raw_size": cur.get("raw_size", raw_size),
                    "n_docs": n_docs, "n_browsable": n_browsable,
                    "reconciliation": recon, "ts": int(time.time())})
                client.put_object(
                    Bucket=bucket, Key=POINTER_KEY, Body=pointer.encode(),
                    ContentType="application/json", CacheControl="no-store, max-age=0")
                if verbose:
                    print("publish-db: unchanged DB — re-stamped pointer with fresh "
                          "reconciliation (force_pointer)", flush=True)
                return {"key": cur.get("key", key), "sha256": digest, "size": cur.get("size", 0),
                        "n_docs": n_docs, "n_browsable": n_browsable, "reconciliation": recon,
                        "kept": kept if only_uploaded else None, "restamped": True}
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
                        "n_docs": n_docs, "n_browsable": n_browsable, "reconciliation": recon,
                        "kept": kept if only_uploaded else None, "guarded": True}
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
                              "raw_size": raw_size, "n_docs": n_docs,
                              "n_browsable": n_browsable, "reconciliation": recon,
                              "ts": int(time.time())})
        client.put_object(
            Bucket=bucket, Key=POINTER_KEY, Body=pointer.encode(),
            ContentType="application/json", CacheControl="no-store, max-age=0",
        )
    return {"key": key, "sha256": digest, "size": gz_size, "n_docs": n_docs,
            "n_browsable": n_browsable, "reconciliation": recon,
            "kept": kept if only_uploaded else None}


# --- multi-writer merge (local crop machines) --------------------------------

def _table_columns(con: sqlite3.Connection, table: str, *, alias: str = "") -> list[str]:
    """Bare column names of ``table`` (excludes AUTOINCREMENT ``id``), preserving table order.

    ``alias`` reads an attached DB (e.g. ``remote``) via ``PRAGMA <alias>.table_info(<table>)``.
    """
    pragma = f"PRAGMA {alias}.table_info({table})" if alias else f"PRAGMA table_info({table})"
    return [r[1] for r in con.execute(pragma) if r[1] != "id"]


def _shared_columns(con: sqlite3.Connection, table: str) -> list[str]:
    """Columns present in BOTH ``main.<table>`` and the attached ``remote.<table>``.

    Merging on the intersection (not ``SELECT *`` / the local column list) is what lets a
    *fat* local writer DB ingest a *slim* published serving snapshot — and vice versa —
    without an ``OperationalError: no such column`` or a positional column misalignment.
    Local column order is preserved so INSERT and SELECT lists line up.
    """
    remote_cols = set(_table_columns(con, table, alias="remote"))
    return [c for c in _table_columns(con, table) if c in remote_cols]


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
        # Explicit shared-column list (never SELECT *): local and remote may differ in shape
        # (fat writer DB vs slim serving snapshot), so a positional copy would misalign.
        doc_cols = _shared_columns(con, "documents")
        doc_list = ", ".join(doc_cols)
        con.execute(
            f"INSERT OR IGNORE INTO main.documents ({doc_list}) "
            f"SELECT {doc_list} FROM remote.documents"
        )
        docs_added = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] - docs_before
        # vote_fields: same intersection rule. Unqualified SELECT names resolve against
        # remote.vote_fields per the FROM clause.
        vf_cols = _shared_columns(con, "vote_fields")
        vf_list = ", ".join(vf_cols)
        fields_added = 0
        if pending and vf_cols:
            before_vf = con.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0]
            placeholders = ", ".join("?" for _ in pending)
            con.execute(
                f"INSERT INTO main.vote_fields ({vf_list}) "
                f"SELECT {vf_list} FROM remote.vote_fields "
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


def backup_published_db(
    dest_dir: Path,
    *,
    cdn_base: str | None = None,
    bucket: str | None = None,
    client=None,
    timeout: float = 300.0,
) -> dict | None:
    """Download the live published snapshot to ``dest_dir`` as a DR copy *outside* the bucket.

    R1 is the permanent historical record but today lives in a single Tigris bucket; this writes
    one cheap off-Tigris copy (the decompressed, sha-verified DB + the pointer JSON sidecar) that
    an operator can park on another provider / external drive. Returns ``{path, sha256, n_docs}``
    or None when nothing is published yet. The sha is verified on download (raises on mismatch).
    """
    pointer = fetch_published_pointer(cdn_base=cdn_base or None, bucket=bucket, client=client, timeout=timeout)
    if pointer is None:
        return None
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    sha = pointer.get("sha256", "")
    dest = dest_dir / f"results-{sha[:16]}.sqlite"
    _download_snapshot_file(pointer, dest, cdn_base=cdn_base or None, bucket=bucket, client=client, timeout=timeout)
    (dest_dir / "latest.json").write_text(json.dumps(pointer), encoding="utf-8")
    return {"path": str(dest), "sha256": sha, "n_docs": pointer.get("n_docs")}


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
            # None (not 0) for pre-reconciliation pointers so the admin board shows "—" rather
            # than a false "0 browsable" / a bogus divergence against a real served count.
            "n_browsable": p.get("n_browsable"),
            # The full count-model chain stamped by the publisher (None on legacy pointers).
            "reconciliation": p.get("reconciliation"),
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


def _read_lock_via_cdn(base: str, timeout: float) -> dict:
    """Stdlib read of ``db/lock.json`` through the public CDN (the reader image has no boto3).
    Best-effort: any failure returns ``{}`` (treated as unlocked) so a flaky read never freezes
    legitimate updates."""
    sep = "&" if "?" in LOCK_KEY else "?"
    url = f"{base}/{LOCK_KEY}"
    if base.startswith("http"):
        url += f"{sep}t={int(time.time())}"
    try:
        d = json.loads(_fetch(url, timeout))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def refresh_db_once(cdn_base: str, dest_db: Path, *, timeout: float = 60.0) -> str | None:
    """Pull a newer snapshot if the pointer changed; atomic-swap it in.

    Returns the new sha256 if it installed one, else None. Raises only on unexpected I/O
    so the caller can log; partial work is cleaned up and never touches the served file.

    Reader-side lock enforcement: ``db/lock.json`` is otherwise only honored by current-code
    *publishers*, so a stale/old publisher can flip the pointer past it (it happened — a legacy
    publisher overwrote a locked, reconciliation-stamped pointer with an n_docs-less fat DB). The
    reader is current code and decides what is actually SERVED, so it enforces the lock too: while
    locked at N docs, it refuses to adopt a pointer that regresses (n_docs missing or < N), keeping
    the good DB live regardless of what a rogue publisher writes. Fails open on an unreadable lock
    (no worse than before); unlocking, or a grow-past-N publish with the lock updated, passes.
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

    # Lock guard: never let a locked round regress to a smaller/unknown-size published DB.
    lock = _read_lock_via_cdn(base, timeout)
    if lock.get("locked") and dest_db.exists():
        locked_n = lock.get("n_docs")
        new_n = pointer.get("n_docs")
        if isinstance(locked_n, int) and (not isinstance(new_n, int) or new_n < locked_n):
            print(f"refresh-db: REFUSING pointer {want[:12]} (n_docs={new_n}) — DB locked at "
                  f"{locked_n}; keeping the served snapshot (stale/rogue publisher?)", flush=True)
            return None

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
