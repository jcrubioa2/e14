"""Dynamic FastAPI report for community-flagged candidate anomalies."""
from __future__ import annotations

import asyncio
import collections
import functools
import hashlib
import hmac
import ipaddress
import math
import os
import random
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import alerts, config
from .community import (
    PollConfig,
    make_store,
    crop_id,
    field_key_of,
    issue_form_token,
    verify_form_token,
    verify_turnstile,
    voter_token,
)
from .vote_queue import make_publisher

# /browse paginates over ACTAS (grouped), not individual crops.
BROWSE_ACTAS_PER_PAGE = 12
HOTLIST_SIZE = 8
# /reportes pages through ALL reported actas (the billboard is no longer a fixed top-N).
REPORTES_PER_PAGE = 12
# The most-voted actas float silently to the top of /browse (crowd attention compounds).
# Capped well under SQLite's bound-parameter limit so the "exclude these" clause is safe.
VOTED_FLOAT_CAP = 300
# The visible "basis" signal: a CONFIDENT Gemma verdict (not CV — CV was dropped for
# over-firing on plain placeholder dots). The proactive Gemma pass seeds the first
# batch of crops worth reviewing; these are shown ("para revisar") and sorted first.
# UNCLEAR is treated as clean (not shown). Documents Gemma did not screen (most, at a
# 5%% national sample) have a NULL verdict and stay neutral.
_ALGO_FLAG_SQL = (
    "(vf.vlm_classification IN ('SUSPICIOUS_OVERLAP','DIGIT_SHAPE_ANOMALY'))"
)

# One acta-summary row for the /browse list (one entry per document). Reads the precomputed
# per-acta candidate count straight off the small documents table — no join/GROUP BY over the
# 1.5M-row vote_fields table. Every served DB carries n_candidates: DetectorStore maintains it
# incrementally, dbsync.build_serving_db recomputes it into the slim serving snapshot, and
# ensure_n_candidates() backfills it at load for any snapshot that somehow lacks it.
_ACTA_SUMMARY_SELECT = (
    "SELECT d.document_id, d.department_code, d.department_name, "
    "d.municipality_code, d.municipality_name, d.zone, d.puesto, d.mesa, d.place_name, "
    "d.n_candidates AS n_candidates FROM documents d"
)


def ensure_n_candidates(db_path: Path) -> bool:
    """Guarantee the served ``documents`` table carries the precomputed ``n_candidates`` column.

    /browse reads ``d.n_candidates`` to list + filter actas without joining the 1.5M-row
    vote_fields table. A snapshot that ships WITHOUT the column (a raw results.sqlite, or one
    built before the column existed) makes every /browse request a hard 500 (``no such column``).

    Rather than pay a per-request vote_fields scan forever, fix the data ONCE at load: add the
    column if missing and backfill it from vote_fields — the same work build_serving_db does, and
    with the (document_id,row_type) index it's a seconds-long pass, not minutes. Idempotent (a
    no-op when the column is already there) and best-effort: if the file is read-only we log and
    return False rather than crash boot. Returns True iff the column is present afterwards.
    """
    if not db_path.exists():
        return False
    try:
        con = sqlite3.connect(db_path, timeout=60.0)
    except sqlite3.Error as exc:  # noqa: BLE001
        print(f"ensure_n_candidates: open failed: {exc}", flush=True)
        return False
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        if not cols:
            return False  # no documents table at all — nothing to migrate
        if "n_candidates" in cols:
            return True
        con.execute("ALTER TABLE documents ADD COLUMN n_candidates INTEGER NOT NULL DEFAULT 0")
        # Keep the correlated COUNT an index range-probe per acta, not a full vote_fields scan.
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_vf_doc_type ON vote_fields(document_id, row_type)"
        )
        con.execute(
            "UPDATE documents SET n_candidates = COALESCE("
            "(SELECT COUNT(*) FROM vote_fields vf WHERE vf.document_id = documents.document_id "
            "AND vf.row_type='candidate' AND vf.raw_crop_path IS NOT NULL), 0)"
        )
        con.commit()
        print(f"ensure_n_candidates: backfilled n_candidates on {db_path}", flush=True)
        return True
    except sqlite3.Error as exc:  # noqa: BLE001 — never let a migration failure crash boot
        print(f"ensure_n_candidates: backfill failed (serving read-only?): {exc}", flush=True)
        return False
    finally:
        con.close()


def ensure_crop_paths(db_path: Path) -> bool:
    """Self-heal ``candidate`` rows whose ``raw_crop_path`` was dropped by a DB rebuild.

    A rebuild (e.g. the ``recover9`` slim corpus) can leave some candidate rows with a NULL/empty
    ``raw_crop_path`` even though the crop exists — those casillas then vanish from the swipe feed
    and the per-acta grid (both filter ``raw_crop_path IS NOT NULL``), so an acta silently shows
    11/13 instead of 13. The path is deterministic (see ``cropper.save_field_crops``), so we
    reconstruct it from the basename + the crops directory of a sibling non-null row in the SAME
    document — but only when the crop file is actually present on disk.

    Why the file check: a NULL crop path is ambiguous — it may be a *dropped* path (crop exists,
    recoverable) or a *genuine* gap (no crop at all, must stay filtered out so it isn't served as a
    broken card). We can only safely fabricate the former. So this guard self-heals where crops are
    local (the build/repair machine) and is a deliberate no-op on the serve volume (crops live on
    the CDN, not the disk) — which is fine, because a snapshot published after the repair script
    has no NULLs to heal anyway. The authoritative fix (file verification + CDN upload + dedupe) is
    ``scripts.repair_crop_integrity``; this is just belt-and-suspenders against a rebuild re-dropping
    the column on a machine that still has the crops.

    Runs BEFORE ``ensure_n_candidates`` so the count reflects the heal; also recomputes
    ``n_candidates`` for the touched documents itself (ensure_n_candidates short-circuits when the
    column already exists and would not pick up the change). Idempotent + best-effort: a no-op when
    there are no NULL paths, and a read-only file just logs and returns False.
    """
    if not db_path.exists():
        return False
    try:
        con = sqlite3.connect(db_path, timeout=60.0)
    except sqlite3.Error as exc:  # noqa: BLE001
        print(f"ensure_crop_paths: open failed: {exc}", flush=True)
        return False
    try:
        null_rows = con.execute(
            "SELECT id, document_id, page_number, row_number FROM vote_fields "
            "WHERE row_type='candidate' AND (raw_crop_path IS NULL OR raw_crop_path='')"
        ).fetchall()
        # Surface (but never auto-delete) duplicate rows — a regression we want visible.
        dups = con.execute(
            "SELECT COUNT(*) FROM vote_fields v WHERE EXISTS ("
            "SELECT 1 FROM vote_fields v2 WHERE v2.document_id=v.document_id "
            "AND v2.page_number=v.page_number AND v2.row_number=v.row_number "
            "AND v2.row_type=v.row_type AND IFNULL(v2.section,'')=IFNULL(v.section,'') "
            "AND v2.id < v.id)"
        ).fetchone()[0]
        if dups:
            print(f"ensure_crop_paths: WARN {dups} duplicate vote_fields row(s) on {db_path} "
                  "(run scripts.repair_crop_integrity --apply to dedupe)", flush=True)
        if not null_rows:
            return True
        dir_cache: dict[str, str | None] = {}
        updates: list[tuple[str, int]] = []
        touched: set[str] = set()
        for vid, did, page, row in null_rows:
            if did not in dir_cache:
                sib = con.execute(
                    "SELECT raw_crop_path FROM vote_fields WHERE document_id=? AND row_type='candidate' "
                    "AND raw_crop_path IS NOT NULL AND raw_crop_path != '' LIMIT 1",
                    (did,),
                ).fetchone()
                dir_cache[did] = (sib[0].rsplit("/", 1)[0] if sib and "/" in sib[0] else None)
            crops_dir = dir_cache[did]
            if not crops_dir:
                continue  # no sibling to anchor the path; leave NULL
            raw = f"{crops_dir}/{did}_p{page}_row{row}_candidate_field.png"
            if not os.path.exists(raw):
                continue  # genuine gap (or crops not local) — don't fabricate a 404 card
            updates.append((raw, vid))
            touched.add(did)
        if not updates:
            return True
        con.executemany("UPDATE vote_fields SET raw_crop_path=? WHERE id=?", updates)
        # Bump n_candidates for just the touched actas (column already exists -> recompute here).
        con.executemany(
            "UPDATE documents SET n_candidates = COALESCE("
            "(SELECT COUNT(*) FROM vote_fields vf WHERE vf.document_id = documents.document_id "
            "AND vf.row_type='candidate' AND vf.raw_crop_path IS NOT NULL AND vf.raw_crop_path != ''), 0) "
            "WHERE document_id=?",
            [(d,) for d in touched],
        )
        con.commit()
        print(f"ensure_crop_paths: backfilled {len(updates)} crop path(s) across {len(touched)} "
              f"acta(s) on {db_path}", flush=True)
        return True
    except sqlite3.Error as exc:  # noqa: BLE001 — a self-heal must never crash boot
        print(f"ensure_crop_paths: backfill failed (serving read-only?): {exc}", flush=True)
        return False
    finally:
        con.close()


def ensure_browse_indexes(db_path: Path) -> bool:
    """Guarantee the served ``documents`` table carries the indexes /browse's hot path needs.

    /browse orders and paginates with ``WHERE n_candidates>0 ... ORDER BY department_code,
    document_id`` and counts/filters by region. ``dbsync.build_serving_db`` creates the matching
    indexes, but a RAW results.sqlite (the kind that also lacked n_candidates / geo names) ships
    without them — so every page does a full scan + sort over ~122k rows (multi-second TTFB).

    Create them ONCE at load, idempotently. ``idx_doc_browse`` is partial (``WHERE n_candidates>0``)
    so it doubles as both the filter and the sort order for the common case, and lets ``COUNT(*)``
    range-probe instead of scan. Must run AFTER ensure_n_candidates (the partial predicate needs the
    column). Best-effort: a read-only file just logs and returns False rather than crashing boot.
    """
    if not db_path.exists():
        return False
    try:
        con = sqlite3.connect(db_path, timeout=60.0)
    except sqlite3.Error as exc:  # noqa: BLE001
        print(f"ensure_browse_indexes: open failed: {exc}", flush=True)
        return False
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        if "n_candidates" not in cols:
            return False  # partial index needs the column; ensure_n_candidates runs first
        existing = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='documents'"
        )}
        if {"idx_doc_browse", "idx_doc_geo"} <= existing:
            return True
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_doc_browse "
            "ON documents(department_code, document_id) WHERE n_candidates>0"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_doc_geo "
            "ON documents(department_code, municipality_code, zone, puesto)"
        )
        con.execute("ANALYZE documents")  # let the planner actually pick the new indexes
        con.commit()
        print(f"ensure_browse_indexes: built /browse indexes on {db_path}", flush=True)
        return True
    except sqlite3.Error as exc:  # noqa: BLE001 — an index build must never crash boot
        print(f"ensure_browse_indexes: build failed (serving read-only?): {exc}", flush=True)
        return False
    finally:
        con.close()


# The canonical DIVIPOLA dictionary (code -> name for department / municipality / voting place),
# bundled into the package so it ships in the serving image (Dockerfile COPYs e14detector/).
DIVIPOL_DICT_PATH = Path(__file__).resolve().parent / "divipol_dictionary.csv"
ACTA_HASHES_PATH = Path(__file__).resolve().parent / "acta_hashes.sqlite"
# Registraduría E-14 (presidente) PDF base. The official acta URL is fully templated as
# {OFFICIAL_PDF_BASE}/{dep}/{muni}/{zona}/{puesto}/{mesa}/PRE/{hash}.pdf — every part but the
# opaque per-acta hash is encoded in the document_id, so we only store/look up the hash.
OFFICIAL_PDF_BASE = "https://divulgacione14presidente.registraduria.gov.co/assets/temis/pdf"


class GeoNames:
    """In-memory code -> human-name lookup for the geographic hierarchy.

    A published snapshot can carry only the numeric codes (department_code='01',
    municipality_code='001', zone, puesto) with the names left NULL — which would render bare
    codes in /browse. Rather than denormalize a name string onto all ~122k document rows, we
    keep the small canonical mapping (34 departments, ~1.2k municipios) in memory and resolve at
    render time. Lookups return ``None`` on a miss so callers fall back to the code.
    """

    def __init__(self, dept: dict[str, str], muni: dict[tuple[str, str], str],
                 place: dict[tuple[str, str, str, str], str]):
        self._dept, self._muni, self._place = dept, muni, place

    def dept(self, code) -> str | None:
        return self._dept.get(code) if code else None

    def muni(self, dep, code) -> str | None:
        return self._muni.get((dep, code)) if dep and code else None

    def place(self, dep, muni, zona, puesto) -> str | None:
        if not (dep and muni and zona and puesto):
            return None
        return self._place.get((dep, muni, zona, puesto))


@functools.lru_cache(maxsize=4)
def load_geo_names(dict_path: Path = DIVIPOL_DICT_PATH) -> GeoNames:
    """Load the DIVIPOLA dictionary into a GeoNames lookup once (cached). Best-effort: a missing
    or malformed file yields an empty lookup (callers just fall back to showing codes)."""
    import csv
    dept: dict[str, str] = {}
    muni: dict[tuple[str, str], str] = {}
    place: dict[tuple[str, str, str, str], str] = {}
    try:
        with open(dict_path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                dc, mc = r.get("cod_departamento"), r.get("cod_municipio")
                if dc and r.get("departamento"):
                    dept.setdefault(dc, r["departamento"])
                if dc and mc and r.get("municipio"):
                    muni.setdefault((dc, mc), r["municipio"])
                zc, pc, lugar = r.get("cod_zona"), r.get("cod_puesto"), r.get("lugar_votacion")
                if dc and mc and zc and pc and lugar:
                    place.setdefault((dc, mc, zc, pc), lugar)
    except (OSError, csv.Error) as exc:  # noqa: BLE001 — names are a nicety, never crash serving
        print(f"load_geo_names: {exc}", flush=True)
    return GeoNames(dept, muni, place)


def enrich_doc_names(row, geo: GeoNames) -> dict:
    """A dict copy of a documents row with any missing geo NAME filled from the lookup (codes
    untouched). Used wherever a document is rendered, so the served DB stays codes-only."""
    d = dict(row)
    if not d.get("department_name"):
        d["department_name"] = geo.dept(d.get("department_code"))
    if not d.get("municipality_name"):
        d["municipality_name"] = geo.muni(d.get("department_code"), d.get("municipality_code"))
    if "place_name" in d and not d.get("place_name"):
        d["place_name"] = geo.place(
            d.get("department_code"), d.get("municipality_code"), d.get("zone"), d.get("puesto")
        )
    return d


@functools.lru_cache(maxsize=1)
def _acta_hash_conn(path: Path = ACTA_HASHES_PATH) -> "sqlite3.Connection | None":
    """Read-only connection to the bundled document_id -> hash map (~9MB, codes-only snapshots
    ship official_lookup_url NULL). Cached for the process; None if the asset is absent."""
    try:
        if not path.exists():
            return None
        # Shared across requests (and thus threads), unlike the per-request results conn — so it
        # must not be thread-bound. Read-only + SELECT-only, so cross-thread reads are safe.
        return _connect(path, same_thread=False)
    except sqlite3.Error as exc:  # noqa: BLE001 — the link is a nicety, never crash serving
        print(f"_acta_hash_conn: {exc}", flush=True)
        return None


def official_acta_url(document_id: str | None, hash_hex: str) -> str | None:
    """Build the Registraduría PDF URL from the codes encoded in the document_id plus the hash.

    document_id looks like ``E14_PRE_{dep}_{muni}_{zona}_{puesto}_{mesa}_delegados`` — the five
    codes after the ``E14_PRE_`` prefix map straight onto the URL path (already zero-padded)."""
    if not document_id or not hash_hex:
        return None
    parts = document_id.split("_")
    if len(parts) < 7 or parts[0] != "E14":
        return None
    dep, muni, zona, puesto, mesa = parts[2:7]
    return f"{OFFICIAL_PDF_BASE}/{dep}/{muni}/{zona}/{puesto}/{mesa}/PRE/{hash_hex}.pdf"


def official_url_for(document_id: str | None) -> str | None:
    """Resolve a document's official acta URL from the bundled hash map (render-time fallback for
    snapshots that didn't carry official_lookup_url). One indexed lookup; None on any miss."""
    conn = _acta_hash_conn()
    if conn is None or not document_id:
        return None
    try:
        row = conn.execute(
            "SELECT hash FROM acta_hash WHERE document_id=?", (document_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return official_acta_url(document_id, bytes(row[0]).hex())

def _connect(db_path: Path, same_thread: bool = True) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0, check_same_thread=same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


_FLAG_K = 8.0

# Prior strength for the /reportes map's empirical-Bayes adjusted report rate (see _eb_rate).
# Like _FLAG_K, a fixed "pinch of the national average" that keeps tiny samples from screaming.
_MAP_PRIOR_K = 25.0


def _eb_rate(reported: int, reviewed: int, baseline: float, k: float = _MAP_PRIOR_K) -> float:
    """Empirical-Bayes adjusted report rate (reported/reviewed) in 0..1, shrunk toward the national
    ``baseline`` by a fixed-strength Beta prior worth ``k`` mesas. Same philosophy as ``_flag_level``:
    a few reports in a tiny municipio shouldn't read as a wave. With ``reviewed`` small the result
    sits near ``baseline``; as ``reviewed`` grows past ``k`` it converges to the raw rate. A zone with
    ``reviewed == 0`` (so ``reported == 0`` too, since reported ⊆ reviewed) lands exactly on
    ``baseline`` — the map renders it transparent via ``conf`` so it never shows as signal."""
    return (reported + k * baseline) / (reviewed + k)


def _flag_level(strange: int, good: int) -> float:
    """How red an "extraña" marker gets, 0..1. Scales with the NET margin (extrañas minus
    "se ve bien"), NOT the raw ratio — so a single report is a faint blush and the red deepens as
    consensus piles up, instead of jumping to full red on the first vote. ``_FLAG_K`` sets the
    ramp: net == K is ~half red; it saturates toward 1 but never quite screams. 0 when the crowd
    is balanced or leans "se ve bien"."""
    net = strange - good
    return net / (net + _FLAG_K) if net > 0 else 0.0


def _warm_db(db_path: Path) -> None:
    """Pull the served DB's pages into the OS page cache by reading the file through.

    The feed samples random rows across the whole DB, so the *first* request after a boot
    or after a sync swaps in a fresh (cold) file would otherwise fault to disk — slow. The
    slim serving DB fits comfortably in RAM, so one sequential read makes every subsequent
    read a memory hit. Best-effort and silent: a warm failure must never affect serving.
    Run this in a thread (it blocks on I/O).
    """
    try:
        with open(db_path, "rb", buffering=0) as fh:
            while fh.read(1 << 22):  # 4 MiB chunks
                pass
    except Exception:  # noqa: BLE001 — warming is purely an optimization
        pass


def _departments(conn: sqlite3.Connection, geo: "GeoNames | None" = None) -> list:
    # Group by code only and take the non-null name (MAX skips NULLs): some docs carry the
    # code without a name, which would otherwise show a duplicate "code-only" option.
    rows = conn.execute(
        """
        SELECT department_code, MAX(department_name) AS department_name
        FROM documents
        WHERE department_code IS NOT NULL AND department_code <> ''
        GROUP BY department_code
        ORDER BY department_code
        """
    ).fetchall()
    if geo is None:
        return rows
    # Fill names the snapshot left NULL from the in-memory lookup (no per-row DB copies).
    return [{"department_code": r["department_code"],
             "department_name": r["department_name"] or geo.dept(r["department_code"])}
            for r in rows]


def _municipios(conn: sqlite3.Connection, department: str | None, geo: "GeoNames | None" = None) -> list:
    if not department:
        return []
    rows = conn.execute(
        "SELECT municipality_code, MAX(municipality_name) AS municipality_name, "
        "MAX(department_code) AS department_code FROM documents "
        "WHERE (department_code=? OR department_name=?) "
        "AND municipality_code IS NOT NULL AND municipality_code <> '' "
        "GROUP BY municipality_code ORDER BY municipality_code",
        (department, department),
    ).fetchall()
    if geo is None:
        return rows
    return [{"municipality_code": r["municipality_code"],
             "municipality_name": r["municipality_name"]
                or geo.muni(r["department_code"], r["municipality_code"])}
            for r in rows]


def _zonas(conn: sqlite3.Connection, department: str | None, municipality: str | None) -> list[sqlite3.Row]:
    if not (department and municipality):
        return []
    return conn.execute(
        "SELECT DISTINCT zone FROM documents "
        "WHERE (department_code=? OR department_name=?) AND (municipality_code=? OR municipality_name=?) "
        "AND zone IS NOT NULL AND zone<>'' ORDER BY zone",
        (department, department, municipality, municipality),
    ).fetchall()


def _puestos(
    conn: sqlite3.Connection, department: str | None, municipality: str | None, zone: str | None
) -> list[sqlite3.Row]:
    if not (department and municipality and zone):
        return []
    return conn.execute(
        "SELECT DISTINCT puesto FROM documents "
        "WHERE (department_code=? OR department_name=?) AND (municipality_code=? OR municipality_name=?) "
        "AND zone=? AND puesto IS NOT NULL AND puesto<>'' ORDER BY puesto",
        (department, department, municipality, municipality, zone),
    ).fetchall()


def crop_prefix(round: str | None = None) -> str:
    """Bucket-key prefix for crops in a round. ``r1`` (default) keeps the LEGACY ``crops/``
    prefix so first-round object keys never move; any other round nests under ``crops/<round>/``.
    Mirrors the ``db/`` round-prefix scheme in ``dbsync._round_prefix``."""
    r = (round or config.ELECTION_ROUND or "r1").strip().lower()
    return "crops/" if r == "r1" else f"crops/{r}/"


def crop_rel(raw_crop_path: str) -> str:
    """The round-agnostic file part after the local ``crops/`` segment of a stored path.

    The local file always lives at ``<output_dir>/crops/<rel>`` regardless of round (one round
    per machine, so no on-disk collision); only the bucket object key is round-scoped.
    """
    s = str(raw_crop_path).replace("\\", "/")
    idx = s.find("crops/")
    return s[idx + len("crops/"):] if idx != -1 else s.lstrip("/")


def crop_obj_name(crop_rel_path: str, secret: str | None = None) -> str:
    """OPAQUE file part of a crop's object key: ``<hmac(crop_rel)>.png``.

    The readable crop path (``E14_PRE_<dept>_<mun>_<zone>_<puesto>_<mesa>_..._candidate_field.png``)
    fully identifies the mesa, so exposing it as the public CDN URL de-anonymizes the acta. A
    secret-keyed HMAC over the *round-agnostic* ``crop_rel`` gives a name that (a) reveals nothing
    about the acta and can't be rainbow-tabled back (the path namespace is small + structured, so a
    plain hash would be reversible — the secret is what resists that), and (b) is deterministic, so
    the uploader and the feed compute the identical name from the same path without any lookup.

    24 hex chars (96 bits) keeps collisions negligible across the ~1.58M-crop national set. The
    secret MUST match on the webapp and every upload machine (see config.CROP_KEY_SECRET).
    """
    secret = config.CROP_KEY_SECRET if secret is None else secret
    digest = hmac.new(secret.encode("utf-8"), crop_rel_path.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:24] + ".png"


def crop_key(raw_crop_path: str, round: str | None = None) -> str:
    """Object-store key for a crop: ``crops/<hmac>.png`` (r1) or ``crops/<round>/<hmac>.png``.

    The file part is an OPAQUE HMAC of the path (see ``crop_obj_name``) so the CDN URL never leaks
    the mesa; round-scoping (the ``crops/`` vs ``crops/<round>/`` prefix) is unchanged. The manifest
    key == bucket object key == the CDN URL path, so this single function carries crops, the upload
    cache, the publish frontier (``_prune_to_uploaded``), and CDN URLs together. The whole pipeline
    is forward-only (path -> key), so the one-way HMAC needs no reverse index.
    """
    return crop_prefix(round) + crop_obj_name(crop_rel(raw_crop_path))


def crop_cdn_url(raw_crop_path: str, cdn_base: str, round: str | None = None) -> str | None:
    """Public URL for a crop on the CDN, or None when no CDN is configured.

    None => caller falls back to the in-app /crop endpoint.
    """
    if not cdn_base:
        return None
    return f"{cdn_base}/{crop_key(raw_crop_path, round)}"


def resolve_crop_path(path: str, output_dir: Path) -> Path:
    output_dir = Path(output_dir).resolve()
    requested = Path(path)
    candidates = [requested] if requested.is_absolute() else [requested, output_dir / requested]
    resolved = None
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            break
        except FileNotFoundError:
            continue
    if resolved is None:
        raise FileNotFoundError(path)
    resolved.relative_to(output_dir)
    return resolved


def parse_field_key(field_key: str) -> tuple[str, int, int, str] | None:
    """Split ``document:page:row:section`` back into parts (document ids carry no colon)."""
    parts = field_key.rsplit(":", 3)
    if len(parts) != 4:
        return None
    document_id, page, row, section = parts
    try:
        return document_id, int(page), int(row), section
    except ValueError:
        return None


GEO_DIR = Path(__file__).resolve().parent / "geo"
MUNICIPIOS_GEOJSON = GEO_DIR / "colombia_municipios.geojson"
DEPARTAMENTOS_GEOJSON = GEO_DIR / "colombia_departamentos.geojson"


def doc_muni_index(db: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """document_id -> (department_code, municipality_code) for browsable actas."""
    rows = db.execute(
        """
        SELECT document_id, department_code, municipality_code
        FROM documents
        WHERE n_candidates > 0
          AND department_code IS NOT NULL AND department_code <> ''
          AND municipality_code IS NOT NULL AND municipality_code <> ''
        """
    ).fetchall()
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        dep = str(r["department_code"]).strip().zfill(2)
        muni = str(r["municipality_code"]).strip().zfill(3)
        out[r["document_id"]] = (dep, muni)
    return out


def municipio_report_stats(
    doc_index: dict[str, tuple[str, str]],
    reported_docs: set[str],
    geo: GeoNames,
    reviewed_docs: set[str] | None = None,
) -> dict[str, Any]:
    """Per-municipio and per-department mesa counts with crowd-reported mesas.

    Each row carries two denominators: ``pct`` = reported/total (every browsable mesa) and
    ``pct_reviewed`` = reported/reviewed (only mesas someone has voted on). The /reportes map
    toggles between them client-side. ``reviewed_docs`` defaults to empty (callers that don't
    pass it get reviewed counts of 0)."""
    reviewed_docs = reviewed_docs or set()
    totals: collections.Counter[tuple[str, str]] = collections.Counter()
    reported: collections.Counter[tuple[str, str]] = collections.Counter()
    reviewed: collections.Counter[tuple[str, str]] = collections.Counter()
    for doc_id, key in doc_index.items():
        totals[key] += 1
        if doc_id in reported_docs:
            reported[key] += 1
        if doc_id in reviewed_docs:
            reviewed[key] += 1

    # National baseline report rate (reported/reviewed) — the mean each zone's adjusted score is
    # shrunk toward, so a 1-of-1 municipio sits near the average instead of at a screaming 100%.
    k = _MAP_PRIOR_K
    tot_reviewed = sum(reviewed.values())
    p0 = sum(reported.values()) / tot_reviewed if tot_reviewed else 0.0

    def _row(name: str, dep: str, muni: str | None, total: int, rep: int, rev: int) -> dict[str, Any]:
        row: dict[str, Any] = {
            "dep": dep,
            "name": name,
            "dep_name": geo.dept(dep) or dep,
            "total": total,
            "reported": rep,
            "reviewed": rev,
            # Literal ratios (kept for tooltips, /transparencia, back-compat).
            "pct": round(100.0 * rep / total, 1) if total else 0.0,
            "pct_reviewed": round(100.0 * rep / rev, 1) if rev else 0.0,
            # Coverage: how much of the zone has been reviewed at all.
            "coverage": round(100.0 * rev / total, 1) if total else 0.0,
            # Sample-size-aware report rate (drives map color + sort) and its confidence (opacity).
            "score": round(100.0 * _eb_rate(rep, rev, p0, k), 1),
            "conf": round(rev / (rev + k), 3),
        }
        if muni is not None:
            row["muni"] = muni
        return row

    municipios: list[dict[str, Any]] = []
    for (dep, muni), total in totals.items():
        municipios.append(_row(
            geo.muni(dep, muni) or muni, dep, muni,
            total, reported.get((dep, muni), 0), reviewed.get((dep, muni), 0),
        ))
    municipios.sort(key=lambda r: (-r["score"], -r["reviewed"], r["dep"], r["muni"]))

    dept_totals: collections.Counter[str] = collections.Counter()
    dept_reported: collections.Counter[str] = collections.Counter()
    dept_reviewed: collections.Counter[str] = collections.Counter()
    for (dep, _muni), total in totals.items():
        dept_totals[dep] += total
        dept_reported[dep] += reported.get((dep, _muni), 0)
        dept_reviewed[dep] += reviewed.get((dep, _muni), 0)

    departments: list[dict[str, Any]] = []
    for dep in sorted(dept_totals.keys()):
        departments.append(_row(
            geo.dept(dep) or dep, dep, None,
            dept_totals[dep], dept_reported[dep], dept_reviewed[dep],
        ))
    departments.sort(key=lambda r: (-r["score"], -r["reviewed"], r["dep"]))

    total_mesas = sum(dept_totals.values())
    return {
        "municipios": municipios,
        "departments": departments,
        "summary": {
            "total_mesas": total_mesas,
            "reported_mesas": len(reported_docs & doc_index.keys()),
            "reviewed_mesas": len(reviewed_docs & doc_index.keys()),
            "baseline_pct": round(100.0 * p0, 1),
            "coverage_pct": round(100.0 * tot_reviewed / total_mesas, 1) if total_mesas else 0.0,
            "prior_k": k,
        },
    }


def _es_thousands(n: int) -> str:
    """Format an integer with Colombian thousands separators (1234567 -> '1.234.567')."""
    return f"{n:,}".replace(",", ".")


def _es_ago(then: datetime, now: datetime) -> str:
    """Spanish relative time, e.g. 'hace 5 minutos' / 'hace 2 horas'."""
    secs = max(0, int((now - then).total_seconds()))
    if secs < 90:
        return "hace unos segundos"
    mins = secs // 60
    if mins < 60:
        return f"hace {mins} minuto{'s' if mins != 1 else ''}"
    hours = mins // 60
    if hours < 24:
        return f"hace {hours} hora{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"hace {days} día{'s' if days != 1 else ''}"


def _es_duration(secs: float) -> str:
    """Spanish coarse duration, e.g. '~3 h 20 min' / '~45 min'."""
    mins = max(1, int(round(secs / 60)))
    hours, mins = divmod(mins, 60)
    if hours and mins:
        return f"~{hours} h {mins} min"
    if hours:
        return f"~{hours} h"
    return f"~{mins} min"


def compute_sync_progress(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    *,
    national_total: int | None = None,
) -> dict:
    """Public rollout status, derived purely from the served results DB.

    Counts browsable actas (those with at least one candidate crop) against the full national
    universe (``mesas_informadas`` from the count-model reconciliation, passed in as
    ``national_total``), and estimates remaining time from the processing-timestamp span.

    The denominator is no longer a hardcoded constant (the old ``NATIONAL_TOTAL_ACTAS=121913``
    matched nothing and manufactured a fake 100%). When ``national_total`` is unknown we fall
    back to the served count itself so the bar reads an honest "complete" rather than capping
    against a wrong number — and flag it via ``total_known=False``.
    """
    now = now or datetime.now(timezone.utc)
    # Optional env override stays supported, but there is NO baked-in default any more.
    if national_total is None:
        env_total = os.environ.get("E14_NATIONAL_TOTAL")
        national_total = int(env_total) if env_total and env_total.isdigit() else None
    total_known = bool(national_total and national_total > 0)
    # A browsable acta is one with >=1 candidate crop — exactly documents.n_candidates>0 (the
    # precomputed column). Count it off the 122k-row documents table via idx_doc_browse instead of
    # a COUNT(DISTINCT) + JOIN scan over the 1.5M-row vote_fields table (which dominated /browse).
    row = conn.execute(
        "SELECT COUNT(*) AS synced, "
        "       MIN(processing_timestamp) AS first_ts, "
        "       MAX(processing_timestamp) AS last_ts "
        "FROM documents WHERE n_candidates>0"
    ).fetchone()
    served_browsable = row["synced"] or 0  # raw (uncapped) — authoritative "what's actually served"
    # Denominator: the known national target (informadas) when we have it, else the served
    # count itself (honest "complete", no fake cap against a wrong constant).
    total = max(1, national_total if total_known else served_browsable)
    synced = min(served_browsable, total)
    pct = round(synced * 100 / total, 1)
    # Total rows in the served snapshot (incl. n_candidates=0). The served DB on the volume is the
    # ground truth of what visitors get, so the admin board's totales/con-recortes/sin-recortes
    # triplet is derived from HERE (always available) — the pointer is only used to flag a
    # served-vs-published *divergence*. served_gap = the 'sin recortes' rows (n_candidates=0).
    served_total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    served_gap = served_total - served_browsable

    last_sync_text = None
    if row["last_ts"]:
        try:
            last_sync_text = _es_ago(datetime.fromisoformat(row["last_ts"]), now)
        except ValueError:
            last_sync_text = None

    eta_text = None
    if 0 < synced < total and row["first_ts"] and row["last_ts"]:
        try:
            elapsed = (datetime.fromisoformat(row["last_ts"]) - datetime.fromisoformat(row["first_ts"])).total_seconds()
        except ValueError:
            elapsed = 0
        # Need a couple of actas and a real time span to project a rate.
        if synced >= 2 and elapsed > 0:
            remaining_secs = (total - synced) * (elapsed / synced)
            # Suppress implausible estimates (>14 days): the rate sample isn't
            # representative yet (e.g. stale pilot data, or a stalled publisher).
            if remaining_secs <= 14 * 24 * 3600:
                eta_text = _es_duration(remaining_secs)

    return {
        "synced": synced,
        "total": total,
        "synced_label": _es_thousands(synced),
        "total_label": _es_thousands(total),
        "served_browsable": served_browsable,
        "served_browsable_label": _es_thousands(served_browsable),
        "served_total": served_total,
        "served_total_label": _es_thousands(served_total),
        "served_gap": served_gap,
        "served_gap_label": _es_thousands(served_gap),
        "pct": pct,
        "total_known": total_known,
        "complete": synced >= total,
        "last_sync_text": last_sync_text,
        "eta_text": eta_text,
    }


def build_count_chain(recon: dict | None, served_total: int) -> dict:
    """Assemble the count-model chain for display, from the pointer reconciliation block + the
    app's own authoritative served count. Pure/derived so it unit-tests cleanly.

    The chain is non-increasing top→bottom (total_global ≥ informadas ≥ downloaded ≥
    crops_uploaded ≥ sqlite_served == published); any known count exceeding the one above it is
    an inversion (status ``bad``) and an alarm. Unknown counts (a source not reachable from the
    publishing machine) render as ``na`` and don't break the monotone check. ``sqlite_served`` is
    the app's OWN live served count (authoritative); ``published`` is what the pointer claims it
    shipped — they must be equal (``served_eq_published``).
    """
    recon = recon or {}
    total_global = recon.get("total_global")
    escrutadas = recon.get("mesas_escrutadas")
    informadas = recon.get("mesas_informadas")
    downloaded = recon.get("downloaded")
    crops_uploaded = recon.get("crops_uploaded")
    published = recon.get("sqlite_served")  # the pointer's claim of what it shipped
    rows = [
        {"key": "total_global", "label": "Total nacional de mesas", "count": total_global,
         "source": "Resultados oficiales · ACT/PR/00.json (metota)"},
        {"key": "mesas_escrutadas", "label": "Mesas escrutadas", "count": escrutadas,
         "source": "Resultados oficiales · ACT/PR/00.json (mesesc)"},
        {"key": "informadas", "label": "Actas con imagen publicada", "count": informadas,
         "source": "Divulgador · allTransmissionCodes.json"},
        {"key": "downloaded", "label": "Actas descargadas", "count": downloaded,
         "source": "manifest · status=done"},
        {"key": "crops_uploaded", "label": "Recortes subidos (frontera)", "count": crops_uploaded,
         "source": "review/uploaded_crops.txt"},
        {"key": "sqlite_served", "label": "En la DB servida", "count": served_total,
         "source": "results.sqlite · COUNT(documents)"},
        {"key": "published", "label": "Publicadas (lo que ve el público)", "count": published,
         "source": "db/latest.json · n_docs"},
    ]
    prev = None
    for r in rows:
        c = r["count"]
        if c is None:
            r["status"] = "na"
        elif prev is not None and c > prev:
            r["status"] = "bad"  # inversion — a lower row exceeds a higher one
        else:
            r["status"] = "ok"
        if c is not None:
            prev = c
        r["count_label"] = _es_thousands(c) if isinstance(c, int) else "—"
    served_eq_published = published is None or published == served_total
    cobertura = round(served_total * 100 / informadas, 2) if isinstance(informadas, int) and informadas else None
    backlog_ingesta = max(0, informadas - served_total) if isinstance(informadas, int) else None
    backlog_reporte = (max(0, total_global - informadas)
                       if isinstance(total_global, int) and isinstance(informadas, int) else None)
    return {
        "rows": rows,
        "cobertura": cobertura,
        # Colombian decimal comma, e.g. 99.99 -> "99,99".
        "cobertura_label": (f"{cobertura:.2f}".replace(".", ",") if cobertura is not None else None),
        "backlog_ingesta": backlog_ingesta,
        "backlog_ingesta_label": (_es_thousands(backlog_ingesta) if backlog_ingesta is not None else None),
        "backlog_reporte": backlog_reporte,
        "backlog_reporte_label": (_es_thousands(backlog_reporte) if backlog_reporte is not None else None),
        "served_eq_published": served_eq_published,
        "missing_count": recon.get("missing_count"),
        "universe_fetched_at": recon.get("universe_fetched_at"),
        # Content-integrity axis (parallel, informational): latest content-report summary.
        "content": recon.get("content"),
        "has_reconciliation": bool(recon),
    }


def build_public_counts(recon: dict | None, served_total: int) -> dict:
    """The PUBLIC projection of the count model — for citizens, not operators.

    The admin chain (build_count_chain) exposes every internal frontier (downloaded, crops,
    published, inversion flags, source columns). The public doesn't need any of that: it needs a
    plain four-step funnel from "the whole country" to "what you can actually open here", in
    everyday Spanish. Same source numbers, friendly labels, no jargon. ``mesas_escrutadas`` is
    surfaced as "escaneadas" (the wording the public understands), and the final, highlighted step
    is what the platform genuinely makes accessible (the served count).
    """
    recon = recon or {}
    tg = recon.get("total_global")
    esc = recon.get("mesas_escrutadas")
    inf = recon.get("mesas_informadas")

    def lab(n):
        return _es_thousands(n) if isinstance(n, int) else "—"

    # Each step carries a narrative connector so the four numbers read as one sentence top-to-bottom
    # ("En todo el país… de ellas… y de esas… todas, aquí"), rather than a technical pipeline.
    funnel = [
        {"key": "pais", "connector": "En todo el país hay",
         "label": "Mesas instaladas en el país", "value_label": lab(tg),
         "sub": "El total nacional, según la Registraduría."},
        {"key": "escaneadas", "connector": "De ellas, ya fueron escaneadas",
         "label": "Mesas escaneadas", "value_label": lab(esc),
         "sub": "Mesas cuyo resultado ya procesó la Registraduría."},
        {"key": "acta", "connector": "Y de esas, ya tienen su acta publicada",
         "label": "Mesas con su acta (PDF) publicada", "value_label": lab(inf),
         "sub": "La Registraduría ya publicó el PDF del formulario E-14."},
        {"key": "sistema", "connector": "Todas, disponibles para ti aquí",
         "label": "Mesas disponibles en nuestro sistema", "value_label": lab(served_total),
         "sub": "Listas para consultar, revisar y comparar.", "highlight": True},
    ]
    cobertura = round(served_total * 100 / inf, 2) if isinstance(inf, int) and inf else None
    return {
        "served_label": lab(served_total),
        "cobertura_label": (f"{cobertura:.2f}".replace(".", ",") if cobertura is not None else None),
        "funnel": funnel,
        # Plain-language gaps: mesas escaneadas cuya acta aún no publica la Registraduría, and
        # actas publicadas que todavía no incorporamos (our pending ingest — 0 when caught up).
        "sin_acta": (max(0, esc - inf) if isinstance(esc, int) and isinstance(inf, int) else None),
        "pendientes": (max(0, inf - served_total) if isinstance(inf, int) else None),
        "missing_count": recon.get("missing_count"),
        "content": recon.get("content"),
        "universe_fetched_at": recon.get("universe_fetched_at"),
        "has_data": bool(recon) and inf is not None,
    }


def stalled_publisher_msg(
    age_secs: int | None, stale_after: int, served_pct: float | None, complete_pct: float
) -> str | None:
    """Alert text if a stale snapshot pointer looks like a *stalled* rollout, else None.

    A stale pointer (publisher not flipping ``db/latest.json``) is only a problem while the
    rollout is incomplete — once the served set reaches ``complete_pct`` of the national total
    the publisher legitimately stops and the pointer age grows forever, which must NOT page.
    Decided from rollout progress, not a hard switch. ``served_pct=None`` means we couldn't
    measure it -> page anyway (a DB read error must not mask a real stall). ``stale_after<=0``
    disables the check outright (manual escape hatch).
    """
    if stale_after <= 0 or age_secs is None or age_secs <= stale_after:
        return None
    if served_pct is not None and served_pct >= complete_pct:
        return None  # rollout complete — a stale pointer is the expected steady state
    served = "an unknown share" if served_pct is None else f"only {served_pct:.0f}%"
    return (
        f"snapshot pointer idle {age_secs // 60} min and {served} of the national set is "
        f"served — publisher stalled or reader stuck?"
    )


def _voted_doc_rows(
    conn: sqlite3.Connection, popularity: dict[str, int], clause: str, params: list, cap: int
) -> tuple[list[sqlite3.Row], list[str]]:
    """The most-voted actas matching the current filter, ordered by vote count desc.

    These float silently to the top of /browse (no counts shown). Returns the ordered
    rows and the ids that matched (so the 'rest' query can exclude them). Capped so the
    id list stays a safe number of bound parameters.
    """
    if not popularity:
        return [], []
    top_ids = [doc for doc, _ in sorted(popularity.items(), key=lambda kv: kv[1], reverse=True)[:cap]]
    rank = {doc: i for i, doc in enumerate(top_ids)}
    placeholders = ",".join("?" for _ in top_ids)
    rows = conn.execute(
        f"{_ACTA_SUMMARY_SELECT} WHERE {clause} AND d.document_id IN ({placeholders})",
        [*params, *top_ids],
    ).fetchall()
    rows = sorted(rows, key=lambda r: rank[r["document_id"]])
    return rows, [r["document_id"] for r in rows]


def lookup_candidate_appeal(conn: sqlite3.Connection, field_key: str) -> tuple[str, bool] | None:
    """Resolve a field key to (raw_crop_path, is_gemma_seed) for the appeal path."""
    parsed = parse_field_key(field_key)
    if parsed is None:
        return None
    document_id, page, row, section = parsed
    row_ = conn.execute(
        f"SELECT raw_crop_path, CASE WHEN {_ALGO_FLAG_SQL} THEN 1 ELSE 0 END AS algo_flagged "
        "FROM vote_fields vf "
        "WHERE document_id=? AND page_number=? AND row_number=? "
        "AND COALESCE(section,'')=? AND row_type='candidate' LIMIT 1",
        (document_id, page, row, section),
    ).fetchone()
    if row_ is None or not row_["raw_crop_path"]:
        return None
    return row_["raw_crop_path"], bool(row_["algo_flagged"])


_CF_NETS = []
for _c in config.CF_IP_RANGES:
    try:
        _CF_NETS.append(ipaddress.ip_network(_c))
    except ValueError:
        pass


def _ip_in_cloudflare(ip: str) -> bool:
    """True if ``ip`` is within Cloudflare's published edge ranges."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _CF_NETS)


def _client_ip(request: Request) -> str:
    """Real client IP — un-spoofable, and correct whether or not Cloudflare is in front.

    ``Fly-Client-IP`` is set by Fly's edge to whoever opened the connection, and a client cannot
    forge it. If that connector is a Cloudflare IP, the request came through Cloudflare and the real
    visitor is in ``cf-connecting-ip`` — which we trust ONLY in that case, so a direct-to-Fly
    attacker can't forge ``cf-connecting-ip`` (their Fly-Client-IP wouldn't be a Cloudflare IP).
    Otherwise (direct to Fly, or mid-DNS-propagation) Fly-Client-IP IS the real client. We never
    trust the first ``X-Forwarded-For`` hop (attacker-controlled); the XFF/peer fallbacks are only
    for local/dev where no Fly header exists."""
    fly = request.headers.get("fly-client-ip")
    if fly:
        fly = fly.strip()
        if _ip_in_cloudflare(fly):
            cf = request.headers.get("cf-connecting-ip")
            if cf:
                return cf.strip()
        return fly
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "0.0.0.0"


def _voter_ip(request: Request) -> str:
    """Client IP normalized for the voter identity: an IPv6 address is collapsed to its **/64**
    so one allocation (which can hand out billions of addresses) maps to a SINGLE identity, not
    unlimited ones. IPv4 (and any non-IP fallback string) is returned unchanged. Used only for
    ``voter_token`` / rate limiting; Turnstile still gets the raw ``_client_ip`` as remoteip."""
    ip = _client_ip(request)
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    return ip


# In-process per-IP token bucket for the read/deck endpoints. Best-effort and per-machine (not
# shared across Fly machines) — that's fine: its only job is to stop one client from driving
# unbounded cid_index writes / Aurora cost by hammering /api/feed. The authoritative anti-Sybil
# controls (dedup, the vote rate limiter, Turnstile) all live on the write path. Keyed by
# _voter_ip; costs zero DB round-trips so it doesn't add load to the hot read path.
_feed_buckets: dict[str, tuple[float, float]] = {}
_feed_lock = threading.Lock()


def _feed_allow(ip: str) -> bool:
    refill = config.FEED_RATE_REFILL_PER_MIN / 60.0
    cap = config.FEED_RATE_BUCKET
    now = time.time()
    with _feed_lock:
        if len(_feed_buckets) > 50000:
            _feed_buckets.clear()  # crude memory bound under an IP-spray; best-effort anyway
        tokens, last = _feed_buckets.get(ip, (cap, now))
        tokens = min(cap, tokens + (now - last) * refill)
        if tokens < 1.0:
            _feed_buckets[ip] = (tokens, now)
            return False
        _feed_buckets[ip] = (tokens - 1.0, now)
        return True


def _require_admin(request: Request, key: str) -> None:
    """Gate the operator-only /admin routes. 404 when no token is configured (feature off);
    403 unless the supplied token matches. The token may come from an ``X-Admin-Token`` header
    (preferred — keeps it out of URLs/logs/Referer) or the ``?key=`` query param (browser
    convenience). Compared in constant time to avoid leaking it via response timing."""
    if not config.ADMIN_TOKEN:
        raise HTTPException(status_code=404, detail="not found")
    supplied = request.headers.get("x-admin-token") or key or ""
    if not hmac.compare_digest(supplied, config.ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="forbidden")


def create_app(
    results_db: Path,
    output_dir: Path,
    community_db: Path | None = None,
    poll: PollConfig | None = None,
) -> FastAPI:
    results_db = Path(results_db)
    output_dir = Path(output_dir).resolve()
    # Self-heal any candidate rows a rebuild left without a crop path (so no acta silently shows
    # <13 casillas); runs first so the n_candidates backfill below counts the restored rows.
    ensure_crop_paths(results_db)
    # Backfill the precomputed n_candidates column if this snapshot lacks it, so /browse never
    # 500s on `no such column`. No-op for an up-to-date DB; runs again after each db-sync swap.
    ensure_n_candidates(results_db)
    # ...then the /browse indexes (raw snapshots ship without them -> full scans/sorts per page).
    ensure_browse_indexes(results_db)
    # Code -> human-name lookup, loaded once into memory. A snapshot that ships codes-only gets
    # names resolved at render time (see enrich_doc_names) instead of duplicating them per row.
    geo = load_geo_names()
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    # Round-aware appbar (Phase 2): exposed once as Jinja globals so every page's shared appbar can
    # show the "primera vuelta" archive button when this process serves the runoff — no per-route
    # context churn. On the R1 app r1_archive_url is empty, so the button never renders (byte-identical).
    templates.env.globals["election_round"] = config.ELECTION_ROUND
    templates.env.globals["r1_archive_url"] = config.R1_ARCHIVE_URL
    poll_cfg = poll or PollConfig.from_config()
    community = make_store(community_db or (output_dir / "community.sqlite"))
    # Durable vote path: when SQS is configured, votes are enqueued (worker drains to
    # Postgres) instead of written synchronously. None => synchronous write (local/tests).
    vote_publisher = make_publisher()

    # /browse reads the same global crowd aggregates on every hit — acta_popularity (twice:
    # directly and inside the hot billboard), high_voted_fields, and the hot-actas payload. On the
    # Aurora (RDS Data API) backend each is a network round-trip, so one page made ~5 of them and a
    # cold/paused cluster cold-started on the first. Cache them in-process for a few seconds: the
    # directory tolerates slight staleness, collapsing per-request Aurora traffic to ~1 call / TTL.
    _agg_cache: dict[str, tuple[float, Any]] = {}

    def _agg_cached(key: str, fn, ttl: float = 45.0):
        now = time.monotonic()
        hit = _agg_cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        _agg_cache[key] = (now, val)
        return val

    def _pointer_reconciliation() -> dict | None:
        # The count-model chain the publisher stamped into the pointer. Cached generously (5 min):
        # it changes only when the publisher republishes, and resolving it is a network fetch.
        if not config.CDN_BASE_URL:
            return None

        def _fetch():
            from .dbsync import pointer_status
            ptr = pointer_status(config.CDN_BASE_URL)
            return (ptr or {}).get("reconciliation")
        return _agg_cached("pointer_reconciliation", _fetch, ttl=300.0)

    def _national_total() -> int | None:
        # Denominator for the public progress bar: mesas_informadas from the reconciliation block
        # (the single source of truth), or None when no reconciliation pointer exists yet.
        recon = _pointer_reconciliation()
        inf = (recon or {}).get("mesas_informadas")
        return int(inf) if isinstance(inf, int) and inf > 0 else None

    # cid -> {field_key, crop_rel, document_id} is an immutable, append-only mapping (a cid is
    # registered when the feed surfaces it, well before it can be voted on), so resolving it
    # through a process-local LRU drops a per-vote Aurora Data API round-trip on the hot path.
    # Bounded so memory stays flat across the full national crop set; a miss falls back to the
    # store. (Caching an unknown cid's None is fine — bogus cids stay 404.)
    @functools.lru_cache(maxsize=200_000)
    def resolve_cid_cached(cid: str):
        return community.resolve_cid(cid)

    # Per-crop public tallies on the render paths (feed deck, acta page, billboard cards) hit
    # community.counts_among, which on the Aurora backend is a Data API round-trip. A popular
    # acta/feed made one per view, so read QPS to Aurora scaled with page views. Cache each
    # field_key's tally for a few seconds (config.COUNTS_TTL): only keys whose entry is missing
    # or expired are queried, collapsing the hot-path read traffic while staying near-fresh (the
    # vote response is already optimistic). Bounded so memory stays flat across the crop universe.
    _counts_cache: dict[str, tuple[float, dict[str, int]]] = {}
    _COUNTS_CACHE_CAP = 50_000

    def counts_among_cached(field_keys: list[str]) -> dict[str, dict[str, int]]:
        ttl = config.COUNTS_TTL
        if ttl <= 0:
            return community.counts_among(field_keys)
        now = time.monotonic()
        out: dict[str, dict[str, int]] = {}
        stale: list[str] = []
        seen_stale: set[str] = set()
        for k in field_keys:
            hit = _counts_cache.get(k)
            if hit is not None and now - hit[0] < ttl:
                out[k] = dict(hit[1])  # copy so a caller can't mutate the cached tally
            elif k not in seen_stale:
                seen_stale.add(k)
                stale.append(k)
        if stale:
            fresh = community.counts_among(stale)
            for k, tally in fresh.items():
                _counts_cache[k] = (now, tally)
                out[k] = dict(tally)
            # Bound memory: when the cache grows large, drop expired entries (cheap, correct —
            # it's only a cache). Runs only on the (rare) growth past the cap.
            if len(_counts_cache) > _COUNTS_CACHE_CAP:
                for ck in [c for c, (ts, _) in _counts_cache.items() if now - ts >= ttl]:
                    del _counts_cache[ck]
        return out

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Optional: keep the served results DB in sync with the local writer via the
        # object store (national rollout). Off unless E14_DB_SYNC is set, so the pilot
        # and tests are unaffected. Failures only log — they never crash the app.
        sync_task = None
        if os.environ.get("E14_DB_SYNC", "").lower() in ("1", "true", "yes") and config.CDN_BASE_URL:
            from .dbsync import pointer_status, refresh_db_once

            interval = int(os.environ.get("E14_DB_SYNC_INTERVAL", "60"))
            # The published pointer is "stale" if the publisher hasn't flipped it in this long —
            # a silent failure (the app keeps serving the old DB, so neither /health nor the AWS
            # vote alarms would notice). But a stale pointer is ALSO the normal steady state once
            # the rollout finishes (the publisher stops). So we page only when stale AND the
            # rollout is still incomplete (see stalled_publisher_msg / ROLLOUT_COMPLETE_PCT) — no
            # hard switch. E14_POINTER_STALE_SECS=0 disables the check entirely if ever needed.
            stale_after = int(os.environ.get("E14_POINTER_STALE_SECS", str(30 * 60)))

            async def _warm() -> None:
                await asyncio.to_thread(_warm_db, results_db)

            async def _pull() -> str | None:
                # Returns the new sha when a fresh snapshot was swapped in, else None.
                sha = await asyncio.to_thread(refresh_db_once, config.CDN_BASE_URL, results_db)
                if sha:
                    # A freshly pulled snapshot might predate the n_candidates column — backfill
                    # it before serving so /browse stays on the fast precomputed path. (Geo names
                    # need no per-swap work: they resolve from the in-memory lookup at render.)
                    # Heal dropped crop paths first so the n_candidates backfill counts them.
                    await asyncio.to_thread(ensure_crop_paths, results_db)
                    await asyncio.to_thread(ensure_n_candidates, results_db)
                    await asyncio.to_thread(ensure_browse_indexes, results_db)
                return sha

            async def _check_stale() -> None:
                # Page only if the pointer is stale AND the rollout is still incomplete — a stale
                # pointer is expected once publishing finishes (decided from served progress, not
                # a hard switch). stale_after<=0 disables; skip the network fetch when so.
                if stale_after <= 0:
                    return
                ptr = await asyncio.to_thread(pointer_status, config.CDN_BASE_URL)
                age = ptr.get("age_secs") if ptr else None
                if age is None or age <= stale_after:
                    return  # pointer fresh -> publisher active, nothing to do

                def _served_pct() -> float | None:
                    # Progress of the snapshot we actually serve (== what the publisher shipped).
                    try:
                        with _connect(results_db) as db:
                            return compute_sync_progress(
                                db, national_total=_national_total()).get("pct")
                    except Exception:  # noqa: BLE001 — can't measure -> let the helper page
                        return None

                pct = await asyncio.to_thread(_served_pct)
                msg = stalled_publisher_msg(age, stale_after, pct, config.ROLLOUT_COMPLETE_PCT)
                if msg:
                    alerts.notify("db-stale", msg, severity="warn")

            async def _sync_loop() -> None:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        if await _pull():
                            # A new (cold) file was just swapped in — warm it so the next
                            # feed request is a memory hit, not a disk fault.
                            await _warm()
                        await _check_stale()
                    except Exception as exc:  # noqa: BLE001 — never let sync crash serving
                        print(f"db-sync: {exc}", flush=True)
                        alerts.notify("db-sync", f"db-sync loop error: {exc}")

            # Block on an initial pull ONLY if there's no DB to serve yet. If the volume
            # already has one, serve it immediately and refresh in the background — no
            # 30s startup stall (decompressing the snapshot) on every deploy/restart.
            if not results_db.exists():
                try:
                    await asyncio.wait_for(_pull(), timeout=180)
                except Exception as exc:  # noqa: BLE001
                    print(f"db-sync (initial): {exc}", flush=True)
                    alerts.notify("db-sync-initial", f"initial DB pull failed: {exc}")
            # Warm the page cache in the background so the first feed after a boot/deploy
            # doesn't fault to cold disk. Non-blocking: serving starts immediately.
            warm_task = asyncio.create_task(_warm())
            app.state._bg_tasks.add(warm_task)
            warm_task.add_done_callback(app.state._bg_tasks.discard)
            sync_task = asyncio.create_task(_sync_loop())
        yield
        if sync_task is not None:
            sync_task.cancel()
        community.close()

    # Hide the interactive docs / OpenAPI schema in prod (they publish the whole route+param
    # surface). Re-enable locally with E14_EXPOSE_DOCS=1.
    _docs = {} if config.EXPOSE_DOCS else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Revision de posibles irregularidades E-14", lifespan=lifespan, **_docs)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        """Baseline hardening headers on every response. Deliberately NO restrictive
        ``default-src`` CSP (would break the inline JS/Turnstile widget); only frame-ancestors
        (anti-clickjacking) plus the cheap, universally-safe headers."""
        # Edge-bypass guard: when E14_REQUIRE_CF is on, the Fly origin must only be reachable THROUGH
        # Cloudflare. Any request whose un-spoofable Fly-Client-IP is NOT a Cloudflare IP is a direct-
        # to-origin hit and gets a bare 404 — so e14-poll.fly.dev serves no usable page OR api and
        # looks dead to anyone skipping the edge (no origin-confirmation, no direct-origin vote path).
        # Exemption: /health, which Fly's own internal checker hits directly (no Cloudflare hop), so it
        # must stay reachable or the machine is marked unhealthy. Fail-OPEN when the switch is off
        # (local/dev). The 404 body matches FastAPI's default so a direct hit looks like a plain miss.
        if config.REQUIRE_CF_ORIGIN and request.url.path != "/health":
            _fly = request.headers.get("fly-client-ip", "")
            if not (_fly and _ip_in_cloudflare(_fly)):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
        try:
            resp = await call_next(request)
        except Exception as exc:  # noqa: BLE001 — record + page, then let the 500 propagate
            _record_error(f"unhandled {request.method} {request.url.path}: {exc}")
            alerts.notify("http-exc", f"unhandled error on {request.method} {request.url.path}: {exc}")
            raise
        if resp.status_code >= 500:
            _record_error(f"{resp.status_code} on {request.method} {request.url.path}")
            alerts.notify("http-5xx", f"{resp.status_code} on {request.method} {request.url.path}")
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp

    def _record_error(detail: str) -> None:
        app.state.err_5xx += 1
        app.state.recent_errors.append({"ts": time.time(), "detail": detail})

    app.state.results_db = results_db
    app.state.output_dir = output_dir
    app.state.community = community
    app.state.poll = poll_cfg
    app.state._bg_tasks: set[asyncio.Task] = set()
    # Server-error tracking for the admin health board (and the source of the 5xx alerts).
    app.state.started_at = time.time()
    app.state.err_5xx = 0
    app.state.recent_errors: collections.deque = collections.deque(maxlen=20)
    # Client-reported outbox health (vote-loss signal): how many times a browser couldn't deliver
    # queued votes. Best-effort, in-memory (per machine, resets on deploy) — watch it in /health.
    app.state.telemetry: collections.Counter = collections.Counter()

    def conn() -> sqlite3.Connection:
        if not results_db.exists():
            raise HTTPException(status_code=500, detail=f"results DB not found: {results_db}")
        return _connect(results_db)

    def cid_for(field_key: str, crop_rel: str, document_id: str) -> str:
        """Anonymized public id for a crop, registered so /c/{cid} can resolve it later.

        Every place that surfaces a crop to the public (feed, billboard, acta siblings)
        funnels through here, so the cid_index always knows how to map the opaque id back
        to its real crop without the client ever seeing the field key, path, or acta id.
        """
        cid = crop_id(poll_cfg.form_token_secret, field_key)
        community.register_cid(cid, field_key, crop_rel, document_id)
        return cid

    @app.get("/")
    async def home():
        # The public landing is the anonymized review feed — the flagship, viral flow.
        # 307 (temporary), NOT 308: the landing used to permanently redirect to /browse, and a
        # 308 is cached by browsers indefinitely. Returning visitors who cached "/ -> /browse"
        # would otherwise never re-ask the server and keep landing on /buscar. A temporary
        # redirect is re-checked every visit, so the default can change safely.
        return RedirectResponse("/votar", status_code=307)

    @app.get("/crop")
    async def crop(path: str):
        try:
            resolved = resolve_crop_path(path, output_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="crop not found")
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="crop path outside output_dir") from exc
        return FileResponse(resolved)

    @app.get("/health")
    async def health():
        """Liveness + readiness for an external uptime monitor (UptimeRobot/Better Stack).

        Returns 200 only when the app is up AND has a results DB with actual documents to serve —
        a row-existence probe catches the 'up but stub/empty DB' incident class that a plain ping
        would miss (and, unlike a file-size floor, is correct for any DB size). Cheap and
        unauthenticated (no secrets in the body). An external watcher is what actually catches a
        total outage; a dead process can't page anyone.
        """
        def _ready() -> bool:
            if not results_db.exists():
                return False
            try:
                with _connect(results_db) as db:
                    return db.execute("SELECT 1 FROM documents LIMIT 1").fetchone() is not None
            except Exception:  # noqa: BLE001 — any DB error => not ready
                return False

        ok = await asyncio.to_thread(_ready)
        return JSONResponse(
            {"status": "ok" if ok else "unavailable", "db": ok,
             # Vote-loss signal: client outbox stalls reported since this machine booted.
             "outbox_stalled": app.state.telemetry.get("outbox_stalled", 0)},
            status_code=200 if ok else 503,
        )

    @app.post("/api/telemetry")
    async def api_telemetry(request: Request, payload: dict = Body(...)):
        # Tiny, unauthenticated client telemetry (vote-loss signal). Rate-limited per IP so it
        # can't be spammed to skew the counter; never errors (best-effort, returns 200 always).
        if _feed_allow("tlm:" + _voter_ip(request)):
            event = str(payload.get("event", "") or "")[:40]
            if event:
                app.state.telemetry[event] += 1
                if event == "outbox_stalled":
                    queued = int(payload.get("queued", 0) or 0)
                    print(f"telemetry outbox_stalled queued={queued} ip={_voter_ip(request)}", flush=True)
        return JSONResponse({"ok": True}, status_code=200)

    def _admin_health(votes_ok: bool, pipeline: dict) -> dict:
        """At-a-glance status for the operator board: serving DB, votes backend, queue backlog,
        recent server errors, runtime. Read-only; every probe is best-effort."""
        try:
            size_mb = round(results_db.stat().st_size / 1e6)
        except OSError:
            size_mb = 0
        recent = list(app.state.recent_errors)[-5:][::-1]
        return {
            "db_present": size_mb > 1,
            "db_size_mb": size_mb,
            "pointer_age_min": pipeline.get("pointer_age_min"),
            "pointer_stale": pipeline.get("pointer_stale", False),
            "votes_ok": votes_ok,
            "publisher": vote_publisher is not None,
            "sqs": (vote_publisher.queue_depth() if vote_publisher is not None else None),
            "err_5xx": app.state.err_5xx,
            "recent_errors": recent,
            "uptime_min": round((time.time() - app.state.started_at) / 60),
            "region": os.environ.get("FLY_REGION", ""),
            "machine": os.environ.get("FLY_MACHINE_ID", "")[:8],
            "counts_cache": len(_counts_cache),
            "alerts_on": alerts.configured(),
        }

    def _votes_backend_ok() -> bool:
        # Cheap liveness probe (no row-by-row overview): an empty counts_among round-trips to
        # the votes backend and returns fast. Any error = backend down.
        try:
            community.counts_among([])
            return True
        except Exception as exc:  # noqa: BLE001
            alerts.notify("votes-backend", f"votes backend probe failed: {exc}")
            return False

    @app.get("/admin/poll")
    async def admin_poll(request: Request, key: str = ""):
        # Operator-only: DB/publishing management + health. Off unless a token is configured.
        _require_admin(request, key)
        votes_ok = await asyncio.to_thread(_votes_backend_ok)
        # Pipeline health: what the served DB holds vs what the publisher last shipped.
        # A large pointer age, or a served count far below the published frontier, means
        # the publisher stalled or the reader isn't swapping (the stub-DB incident class).
        from .dbsync import pointer_status, read_db_lock
        ptr = pointer_status(config.CDN_BASE_URL) if config.CDN_BASE_URL else None
        recon = (ptr or {}).get("reconciliation")
        national_total = None
        if recon and isinstance(recon.get("mesas_informadas"), int):
            national_total = recon["mesas_informadas"]
        with conn() as db:
            pipeline = compute_sync_progress(db, national_total=national_total)
        # The count-model chain: one top-to-bottom reconciliation built from the pointer block +
        # the app's own authoritative served count (asserts published == served).
        chain = build_count_chain(recon, pipeline["served_total"])
        pipeline["chain"] = chain
        if ptr:
            age = ptr.get("age_secs")
            pipeline["pointer_sha"] = ptr["sha"]
            pipeline["pointer_raw_mb"] = round(ptr["raw_size"] / 1e6) if ptr.get("raw_size") else None
            pipeline["pointer_gz_mb"] = round(ptr["gz_size"] / 1e6, 1) if ptr.get("gz_size") else None
            pipeline["pointer_n_docs"] = ptr.get("n_docs") or None
            pipeline["pointer_n_browsable"] = ptr.get("n_browsable")
            pipeline["pointer_age_min"] = round(age / 60) if age is not None else None
            # Stale if the publisher hasn't flipped the pointer in >30 min (cycles run ~10-14).
            pipeline["pointer_stale"] = age is not None and age > 30 * 60
            # Divergence = the served DB (authoritative — what visitors actually get) disagrees with
            # what the publisher's pointer claims it shipped, i.e. the publisher stalled or the reader
            # hasn't swapped. Compare totals always (every pointer has n_docs) and browsable when the
            # pointer carries it (post-reconciliation pointers; legacy pointers simply skip that half,
            # never a false alarm). The triplet itself is served-derived, so it renders even on a
            # legacy/locked pointer that predates n_browsable.
            pn_docs = pipeline["pointer_n_docs"]
            pn_browsable = pipeline["pointer_n_browsable"]
            if pn_docs is not None and pipeline["served_total"] != pn_docs:
                pipeline["count_divergence"] = abs(pipeline["served_total"] - pn_docs)
                pipeline["divergence_kind"] = "totales"
            elif pn_browsable is not None and pipeline["served_browsable"] != pn_browsable:
                pipeline["count_divergence"] = abs(pipeline["served_browsable"] - pn_browsable)
                pipeline["divergence_kind"] = "con recortes"
        # Off the event loop: _admin_health and the lock read do blocking network probes.
        health = await asyncio.to_thread(_admin_health, votes_ok, pipeline)
        lock = await asyncio.to_thread(read_db_lock)
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"key": key, "pipeline": pipeline, "health": health, "lock": lock},
        )

    @app.get("/admin/gap")
    async def admin_gap(request: Request, key: str = ""):
        """The ingesta backlog: mesas the Registraduría reports as informed but that we are not
        yet serving (informadas − sqlite_served). Sourced from the pointer reconciliation block's
        missing-key sample so an operator can see *which* mesas are behind, with no shell."""
        _require_admin(request, key)
        recon = await asyncio.to_thread(_pointer_reconciliation)
        recon = recon or {}
        sample = recon.get("missing_keys_sample") or []
        actas: list[dict] = []
        for k in sample:
            parts = str(k).split("_")
            dep, muni, zona, puesto, mesa = (parts + [""] * 5)[:5]
            actas.append({"key": k, "department_code": dep, "municipality_code": muni,
                          "zone": zona, "puesto": puesto, "mesa": mesa})
        return JSONResponse({
            "count": recon.get("missing_count", len(actas)),
            "shown": len(actas),
            "actas": actas,
            "note": "mesas informadas por la Registraduría aún no servidas (backlog de ingesta)",
        })

    @app.post("/admin/db-lock")
    async def admin_db_lock(request: Request, key: str = "", locked: str = ""):
        # Toggle the publish lock (freezes the served DB so no publisher can overwrite it).
        # POST with query params (?key=...&locked=on|off) — no form body, so no multipart dep.
        _require_admin(request, key)
        from .dbsync import set_db_lock
        want = locked.strip().lower() in ("1", "true", "on", "yes", "lock")
        n_docs = None
        if config.CDN_BASE_URL:
            from .dbsync import pointer_status
            ptr = await asyncio.to_thread(pointer_status, config.CDN_BASE_URL)
            n_docs = (ptr or {}).get("n_docs") or None
        lock = await asyncio.to_thread(
            set_db_lock, want, reason="admin console toggle", n_docs=n_docs, by="admin")
        return JSONResponse({"ok": True, "lock": lock})

    @app.get("/robots.txt")
    async def robots():
        body = (
            "User-agent: *\n"
            "Allow: /votar\n"
            "Allow: /reportes\n"
            "Allow: /buscar\n"
            "Allow: /transparencia\n"
            "Allow: /acta/\n"
            "Disallow: /admin\n"
            "Disallow: /api/\n"
            "Disallow: /crop\n"
            f"Sitemap: {config.SITE_URL}/sitemap.xml\n"
        )
        return PlainTextResponse(body)

    @app.get("/sitemap.xml")
    async def sitemap():
        # Entry points + one URL per department (drill-down covers the rest); not every
        # acta — there are >100k and crawlers reach them via links.
        with conn() as db:
            deps = _departments(db)
        from urllib.parse import quote as _quote
        locs = [
            f"{config.SITE_URL}/votar",
            f"{config.SITE_URL}/reportes",
            f"{config.SITE_URL}/buscar",
            f"{config.SITE_URL}/buscar?review=1",
            f"{config.SITE_URL}/transparencia",
        ]
        for d in deps:
            code = d["department_code"] or d["department_name"]
            if code:
                locs.append(f"{config.SITE_URL}/buscar?department={_quote(str(code))}")
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
            + "</urlset>"
        )
        return Response(body, media_type="application/xml")

    @app.get("/api/places")
    async def api_places(
        department: str | None = None,
        municipality: str | None = None,
        zone: str | None = None,
    ):
        # Cascading drill-down options for the NEXT level — cheap (DISTINCT over the small
        # documents table), so the dropdowns update without reloading the whole /browse page.
        with conn() as db:
            if department and municipality and zone:
                opts = [{"value": r["puesto"], "label": r["puesto"]} for r in _puestos(db, department, municipality, zone)]
            elif department and municipality:
                opts = [{"value": r["zone"], "label": r["zone"]} for r in _zonas(db, department, municipality)]
            elif department:
                opts = [
                    {"value": r["municipality_code"] or r["municipality_name"],
                     "label": f"{r['municipality_code'] or ''} {r['municipality_name'] or ''}".strip()}
                    for r in _municipios(db, department, geo)
                ]
            else:
                opts = []
        return {"options": opts}

    def _progress_ctx():
        # National-load progress for the shared app bar. Cached (a cheap precomputed COUNT);
        # opening a conn on a cache hit is negligible. The denominator (mesas_informadas) comes
        # from the reconciliation block, itself cached, so the bar shows the true national share.
        with conn() as db:
            return _agg_cached(
                "sync_progress",
                lambda: compute_sync_progress(db, national_total=_national_total()),
            )

    def _total_reviews():
        # Community impact for the shared app bar: total mesas reviewed. A heavier aggregate (a
        # distinct scan over the vote tables), so cache it generously — "impact" tolerates being a
        # few minutes stale, and this keeps it off the per-request hot path.
        return _agg_cached("total_reviews", community.total_reviews, ttl=300.0)

    def _search_context(department, municipality, zone, puesto, q, review, page):
        # LEVEL 1: a browsable directory, one entry per ACTA (a polling table). Drill down
        # department -> municipio -> zona -> puesto (like the official site). Acta identity
        # IS visible here (this is the lookup view; anonymization is only for /votar). Actas
        # the crowd has voted on float to the top. ``review=1`` narrows to just those.
        # Cascading: ignore a child filter whose parent isn't set.
        if not department:
            municipality = zone = puesto = None
        elif not municipality:
            zone = puesto = None
        elif not zone:
            puesto = None
        # Only actas that actually have candidate crops (precomputed; no vote_fields join).
        where = ["d.n_candidates>0"]
        params: list[Any] = []
        if department:
            where.append("(d.department_code=? OR d.department_name=?)")
            params.extend([department, department])
        if municipality:
            where.append("(d.municipality_code=? OR d.municipality_name=?)")
            params.extend([municipality, municipality])
        if zone:
            where.append("d.zone=?")
            params.append(zone)
        if puesto:
            where.append("d.puesto=?")
            params.append(puesto)
        if q:
            where.append("(d.document_id LIKE ? OR d.place_name LIKE ? OR d.municipality_name LIKE ?)")
            needle = f"%{q}%"
            params.extend([needle, needle, needle])
        clause = " AND ".join(where)
        offset = (page - 1) * BROWSE_ACTAS_PER_PAGE
        region_order = "ORDER BY d.department_code, d.document_id"
        with conn() as db:
            popularity = _agg_cached("popularity", community.acta_popularity)
            if review:
                # Only actas the crowd has voted on, most-voted first. A bounded set, so
                # order + paginate in Python.
                id_list = list(popularity)
                ph = ",".join("?" for _ in id_list) or "NULL"
                rows = db.execute(
                    f"{_ACTA_SUMMARY_SELECT} WHERE {clause} AND d.document_id IN ({ph})",
                    [*params, *id_list],
                ).fetchall()
                rows.sort(key=lambda r: (
                    -popularity.get(r["document_id"], 0),
                    r["department_code"] or "", r["document_id"],
                ))
                total_actas = len(rows)
                doc_rows = rows[offset : offset + BROWSE_ACTAS_PER_PAGE]
            else:
                total_actas = db.execute(
                    f"SELECT COUNT(*) c FROM documents d WHERE {clause}",
                    params,
                ).fetchone()["c"]
                # The most-voted actas float to the very top (crowd attention compounds),
                # then the rest by region. Voted rows are a paginated prefix; the "rest"
                # query excludes them.
                voted_rows, voted_ids = _voted_doc_rows(db, popularity, clause, params, VOTED_FLOAT_CAP)
                head = voted_rows[offset : offset + BROWSE_ACTAS_PER_PAGE]
                rest_rows: list = []
                rest_count = BROWSE_ACTAS_PER_PAGE - len(head)
                if rest_count > 0:
                    rest_clause, rest_params = clause, list(params)
                    if voted_ids:
                        rest_clause += f" AND d.document_id NOT IN ({','.join('?' for _ in voted_ids)})"
                        rest_params += voted_ids
                    rest_rows = db.execute(
                        f"{_ACTA_SUMMARY_SELECT} WHERE {rest_clause} "
                        f"{region_order} LIMIT ? OFFSET ?",
                        [*rest_params, rest_count, max(0, offset - len(voted_rows))],
                    ).fetchall()
                doc_rows = list(head) + list(rest_rows)
            # "Ver todas" (review) renders the same billboard tile (thumb + loc + tally) as the
            # "Actas más reportadas" strip — build a card per acta on this page (bounded to
            # BROWSE_ACTAS_PER_PAGE). Each card costs a vote_fields lookup + a counts_among
            # round-trip, so cache the page's cards for the same 45s window as the other crowd
            # aggregates (keyed by filters+page; tallies tolerate slight staleness).
            voted_cards = []
            if review:
                ck = "voted_cards:" + "|".join(
                    str(x or "") for x in (department, municipality, zone, puesto, q, page))
                voted_cards = _agg_cached(ck, lambda: [
                    c for c in (
                        _acta_card(db, r["document_id"], popularity.get(r["document_id"], 0), r)
                        for r in doc_rows
                    ) if c
                ])
            departments = _departments(db, geo)
            # Dependent drop-downs: each level is populated only once its parent is chosen.
            municipios = _municipios(db, department, geo)
            zonas = _zonas(db, department, municipality)
            puestos = _puestos(db, department, municipality, zone)
            # Graded "extraña" tone for the directory cards — same signal as the billboard. Only
            # the voted actas need a lookup (the long unvoted tail is 0); bounded to the page and
            # batched into a single counts call.
            flag_levels = _acta_flag_levels(
                db, [r["document_id"] for r in doc_rows if popularity.get(r["document_id"])])
        high_voted_docs = {k.rsplit(":", 3)[0] for k in _agg_cached(
            "high_voted", lambda: community.high_voted_fields(config.HIGH_VOTE_THRESHOLD))}
        actas = [
            {
                # Resolve any missing geo names from the in-memory lookup (DB stays codes-only).
                "doc": enrich_doc_names(r, geo),
                "n_candidates": r["n_candidates"],
                "high_voted": r["document_id"] in high_voted_docs,
                "flag_level": flag_levels.get(r["document_id"], 0.0),
            }
            for r in doc_rows
        ]
        return {
            "actas": actas,
            "voted_cards": voted_cards,
            "departments": departments,
            "municipios": municipios,
            "zonas": zonas,
            "puestos": puestos,
            "filters": {
                "department": department or "",
                "municipality": municipality or "",
                "zone": zone or "",
                "puesto": puesto or "",
                "q": q or "",
            },
            "review": bool(review),
            "page": page,
            "pages": max(1, math.ceil(total_actas / BROWSE_ACTAS_PER_PAGE)),
            "total": total_actas,
        }

    @app.get("/browse")
    async def browse_redirect(request: Request):
        # Back-compat: /browse was split into /buscar (search) + /reportes (global reports).
        qs = request.url.query
        # A *bare* /browse (no query) is almost always a returning visitor whose browser cached the
        # old permanent "/ -> /browse" landing redirect (308, never re-checked). Send them to the
        # flagship feed /votar (307, temporary) so the home experience is correct again. Real
        # directory links carry a query (?review=1, ?department=…) -> keep those on /buscar (308).
        if not qs:
            return RedirectResponse("/votar", status_code=307)
        return RedirectResponse("/buscar?" + qs, status_code=308)

    @app.get("/reportes")
    async def reportes(request: Request, page: int = Query(1, ge=1)):
        # TAB 2 — Global reports: the paginated "most reported as extraña" billboard + room for
        # future statistics. Pages through every reported acta, not just a fixed top-N.
        # Clamp the page to the real page count BEFORE it becomes a cache key: _agg_cache is an
        # unbounded, never-evicted process dict, so keying on a raw ?page= would let "?page=<huge>"
        # mint endless entries. Clamping bounds the distinct keys to the actual number of pages
        # (which the popularity dict — already cached, also used by /buscar?review=1 — gives cheaply).
        pages = max(1, math.ceil(len(_agg_cached("popularity", community.acta_popularity)) / REPORTES_PER_PAGE))
        page = max(1, min(page, pages))
        board = _agg_cached(f"hot_actas_p{page}", lambda: _hot_actas_page(page))
        canon = config.SITE_URL + "/reportes" + (f"?page={board['page']}" if board["page"] > 1 else "")
        return templates.TemplateResponse(
            request,
            "reportes.html",
            {
                "hot_actas": board["cards"],
                "page": board["page"],
                "pages": board["pages"],
                "total": board["total"],
                "progress": _progress_ctx(),
                "total_reviews": _total_reviews(),
                "active": "reportes",
                "site_url": config.SITE_URL,
                "canonical": canon,
                "page_title": "Reportes — actas más reportadas como extrañas | Veeduría ciudadana E-14 2026",
                "meta_description": (
                    "Las mesas donde más gente ha marcado casillas como extrañas en las actas "
                    "E-14 de las elecciones de Colombia 2026. Reportes de la veeduría ciudadana."
                ),
            },
        )

    def _map_stats_all() -> dict[str, Any]:
        pop = _agg_cached("popularity", community.acta_popularity)
        reported = set(pop.keys())
        reviewed = _agg_cached("review_popularity", community.reviewed_actas)

        def build() -> dict[str, Any]:
            with conn() as db:
                idx = doc_muni_index(db)
            return municipio_report_stats(idx, reported, geo, reviewed_docs=reviewed)

        return _agg_cached("map_stats_all", build)

    @app.get("/api/reportes/map")
    async def api_reportes_map(department: str | None = None):
        """Choropleth stats: national department rollups or per-municipio drill-down."""
        stats = _map_stats_all()
        if department:
            dep = str(department).strip().zfill(2)
            munis = [m for m in stats["municipios"] if m["dep"] == dep]
            depts = [d for d in stats["departments"] if d["dep"] == dep]
            return {
                "view": "municipios",
                "department": dep,
                "departments": depts,
                "municipios": munis,
                "summary": stats["summary"],
            }
        return {
            "view": "departments",
            "department": None,
            "departments": stats["departments"],
            "municipios": stats["municipios"],
            "summary": stats["summary"],
        }

    @app.get("/geo/colombia_municipios.geojson")
    async def geo_municipios():
        if not MUNICIPIOS_GEOJSON.is_file():
            raise HTTPException(status_code=404, detail="map data not found")
        return FileResponse(MUNICIPIOS_GEOJSON, media_type="application/geo+json")

    @app.get("/geo/colombia_departamentos.geojson")
    async def geo_departamentos():
        if not DEPARTAMENTOS_GEOJSON.is_file():
            raise HTTPException(status_code=404, detail="map data not found")
        return FileResponse(DEPARTAMENTOS_GEOJSON, media_type="application/geo+json")

    @app.get("/transparencia")
    async def transparencia(request: Request):
        # Public, read-only count reconciliation: the SAME chain the admin board renders, so any
        # citizen can verify our counts independently (no token). Reuses the pointer reconciliation
        # block + the app's own served count; asserts published == served.
        recon = _pointer_reconciliation()
        with conn() as db:
            served_total = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        public = build_public_counts(recon, served_total)
        # Parse the missing-key sample into a readable list (only shown when we have a real ingest
        # backlog — mesas with a published acta we haven't incorporated yet).
        backlog = []
        for k in ((recon or {}).get("missing_keys_sample") or []):
            parts = str(k).split("_")
            dep, muni, zona, puesto, mesa = (parts + [""] * 5)[:5]
            backlog.append({"key": k, "dep": dep, "muni": muni, "zona": zona,
                            "puesto": puesto, "mesa": mesa})
        return templates.TemplateResponse(
            request,
            "transparencia.html",
            {
                "public": public,
                "backlog": backlog,
                "progress": _progress_ctx(),
                "total_reviews": _total_reviews(),
                "active": "transparencia",
                "site_url": config.SITE_URL,
                "canonical": config.SITE_URL + "/transparencia",
                "page_title": "Transparencia — conteo de actas E-14 | Veeduría ciudadana 2026",
                "meta_description": (
                    "Cómo cuadran nuestros conteos de actas E-14 con la Registraduría: total "
                    "nacional, mesas informadas, actas servidas y publicadas, cobertura y "
                    "qué mesas faltan. Veeduría ciudadana, elecciones Colombia 2026."
                ),
            },
        )

    @app.get("/buscar")
    async def buscar(
        request: Request,
        department: str | None = None,
        municipality: str | None = None,
        zone: str | None = None,
        puesto: str | None = None,
        q: str | None = None,
        review: int = 0,
        page: int = Query(1, ge=1),
    ):
        # TAB 3 — Search a specific acta: the filterable directory (drill down dep -> muni ->
        # zona -> puesto, like the official site). ``review=1`` narrows to the most-voted list.
        ctx = _search_context(department, municipality, zone, puesto, q, review, page)
        ctx.update({
            "progress": _progress_ctx(),
            "total_reviews": _total_reviews(),
            "active": "buscar",
            "site_url": config.SITE_URL,
            "canonical": config.SITE_URL + ("/buscar?review=1" if review else "/buscar"),
            "page_title": (
                "Actas más votadas por la comunidad — Veeduría ciudadana 2026"
                if review else
                "Busca un acta E-14 — Veeduría ciudadana elecciones Colombia 2026"
            ),
            "meta_description": (
                "Busca y revisa las actas E-14 de las elecciones presidenciales de Colombia "
                "2026 por departamento, municipio o código. Mira los votos escritos a mano y "
                "lo que la comunidad ha votado. Veeduría ciudadana, abierta a todos."
            ),
        })
        return templates.TemplateResponse(request, "buscar.html", ctx)

    @app.get("/acta/{document_id}")
    async def acta_detail(request: Request, document_id: str):
        # LEVEL 2 (read-only): one acta, all candidate crops in ballot order, each showing
        # its PUBLIC community tallies. Voting happens only in the swipe feed (/votar) — this
        # page no longer casts votes, it just displays what the crowd has said.
        with conn() as db:
            doc_row = db.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if not doc_row:
                raise HTTPException(status_code=404, detail="acta no encontrada")
            # Resolve any missing geo names from the in-memory lookup (DB stays codes-only).
            doc = enrich_doc_names(doc_row, geo)
            # Likewise, a codes-only snapshot ships official_lookup_url NULL — rebuild the link
            # to the Registraduría's PDF from the bundled per-acta hash map (template unchanged).
            if not doc.get("official_lookup_url"):
                doc["official_lookup_url"] = official_url_for(doc.get("document_id"))
            frows = db.execute(
                """
                SELECT vf.page_number, vf.row_number, vf.section, vf.candidate_number,
                       vf.candidate_name, vf.raw_crop_path
                FROM vote_fields vf
                WHERE vf.document_id=? AND vf.row_type='candidate'
                  AND vf.raw_crop_path IS NOT NULL
                ORDER BY vf.page_number, vf.row_number
                """,
                (document_id,),
            ).fetchall()
        crops = []
        for fr in frows:
            fkey = field_key_of(document_id, fr["page_number"], fr["row_number"], fr["section"])
            crops.append({
                "row": fr,
                "field_key": fkey,
                "crop_url": crop_cdn_url(fr["raw_crop_path"], config.CDN_BASE_URL),
            })
        counts = counts_among_cached([c["field_key"] for c in crops])
        for c in crops:
            tally = counts.get(c["field_key"], {"good": 0, "strange": 0})
            c["good"] = tally["good"]
            c["strange"] = tally["strange"]
            # Strong crowd signal stands on its own (no model verdict involved anymore).
            c["high_voted"] = tally["strange"] >= config.HIGH_VOTE_THRESHOLD
            # Graded "extraña" tone (0..1): scales with the net margin (extrañas vs "se ve bien"),
            # so a casilla's red ring deepens as more people flag it. See _flag_level.
            c["flag_level"] = _flag_level(c["strange"], c["good"])
        loc = " · ".join(
            x for x in (doc["department_name"] or doc["department_code"],
                        doc["municipality_name"], f"mesa {doc['mesa']}" if doc["mesa"] else None) if x
        ) or doc["document_id"]
        return templates.TemplateResponse(
            request,
            "acta.html",
            {
                "doc": doc,
                "crops": crops,
                "progress": _progress_ctx(),
                "total_reviews": _total_reviews(),
                "active": "buscar",
                "site_url": config.SITE_URL,
                "canonical": f"{config.SITE_URL}/acta/{doc['document_id']}",
                "page_title": f"Acta E-14 — {loc} | Veeduría ciudadana 2026",
                "meta_description": (
                    f"Acta E-14 de {loc}. Mira los votos escritos a mano de cada candidato y "
                    f"lo que la comunidad ha votado sobre cada casilla. Veeduría ciudadana 2026."
                ),
            },
        )

    @app.get("/votar")
    async def votar(request: Request):
        # The headline product: an anonymized, mobile-first swipe feed. The page ships a
        # signed form token (in-app bot check); the deck itself is loaded from /api/feed
        # and votes go to /api/vote.
        sid = request.cookies.get("sid") or uuid.uuid4().hex
        # When Turnstile is on, withhold the form token from the raw page load: the client must
        # solve the challenge and exchange it at /api/session for a token. That gates *starting*
        # to vote on a real-browser proof, with no per-swipe friction. Off => mint inline as before.
        turnstile_on = poll_cfg.turnstile_enabled and bool(poll_cfg.turnstile_sitekey)
        form_token = (
            ""
            if turnstile_on
            else (issue_form_token(poll_cfg.form_token_secret, sid, _voter_ip(request)) if poll_cfg.form_token_secret else "")
        )
        response = templates.TemplateResponse(
            request,
            "swipe.html",
            {
                "form_token": form_token,
                "turnstile_sitekey": poll_cfg.turnstile_sitekey if turnstile_on else "",
                "progress": _progress_ctx(),
                "total_reviews": _total_reviews(),
                "active": "revisar",
                "site_url": config.SITE_URL,
                "canonical": f"{config.SITE_URL}/votar",
                "page_title": "Marca las casillas alteradas — Veeduría ciudadana E-14 2026",
                "meta_description": (
                    "Revisa una mesa completa: mira las casillas de votos escritas a mano, sin "
                    "saber de qué mesa son, y marca las que se vean alteradas. Veeduría "
                    "ciudadana elecciones Colombia 2026."
                ),
            },
        )
        if "sid" not in request.cookies:
            response.set_cookie("sid", sid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
        return response

    @app.get("/c/{cid}")
    async def crop_anon(cid: str):
        # Serve a crop by its opaque id WITHOUT ever revealing the path/acta. Only cids the
        # server has surfaced (and thus registered) resolve; anything else is a 404.
        row = resolve_cid_cached(cid)
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        url = crop_cdn_url(row["crop_rel"], config.CDN_BASE_URL)
        if url:
            return RedirectResponse(url, status_code=307)
        try:
            resolved = resolve_crop_path(row["crop_rel"], output_dir)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(resolved)

    def _feed_payload(n: int, exclude: set[str] | None = None) -> list[dict]:
        """A random batch of anonymized crop cards: ``[{cid, img_url}]``.

        Random order over candidate crops so a voter can't tell which acta a crop is from.
        Registers every cid it hands out so /c/{cid} and /api/vote can resolve them.

        Sampling: pick random primary keys and fetch by id, NOT ``ORDER BY RANDOM()``.
        ``vote_fields`` is ~1.5M candidate rows in a multi-GB DB; ``ORDER BY RANDOM()``
        full-scans + sorts it (≈1s warm, tens of seconds on a cold page cache after a
        deploy). Random-PK is a handful of index seeks (sub-ms) and stays snappy cold.
        ``id`` is a dense AUTOINCREMENT PK so every pick lands on a row; ~3/4 of rows are
        candidates, so we over-sample and top up until we have ``n`` distinct, non-excluded
        cards. Uniform over the id space — plenty random for an anonymized deck.
        """
        exclude = exclude or set()
        out: list[dict] = []
        reg: list[tuple[str, str, str, str]] = []
        seen: set[str] = set(exclude)
        with conn() as db:
            maxid = db.execute("SELECT max(id) AS m FROM vote_fields").fetchone()["m"] or 0
            attempts = 0
            while len(out) < n and maxid and attempts < 8:
                attempts += 1
                ids = [random.randint(1, maxid) for _ in range((n - len(out)) * 4)]
                placeholders = ",".join("?" * len(ids))
                rows = db.execute(
                    f"SELECT document_id, page_number, row_number, section, raw_crop_path "
                    f"FROM vote_fields WHERE id IN ({placeholders}) "
                    f"AND row_type='candidate' AND raw_crop_path IS NOT NULL",
                    ids,
                ).fetchall()
                for r in rows:
                    fkey = field_key_of(
                        r["document_id"], r["page_number"], r["row_number"], r["section"]
                    )
                    cid = crop_id(poll_cfg.form_token_secret, fkey)
                    if cid in seen:
                        continue
                    seen.add(cid)
                    reg.append((cid, fkey, r["raw_crop_path"], r["document_id"]))
                    # Opaque CDN URL straight from the path (no /c redirect hop); the key is an
                    # HMAC so it leaks no acta identity. Falls back to /c/{cid} when no CDN is set.
                    out.append({"cid": cid, "img_url": crop_cdn_url(r["raw_crop_path"], config.CDN_BASE_URL) or f"/c/{cid}"})
                    if len(out) >= n:
                        break
        community.register_cids(reg)
        return out

    @app.get("/api/feed")
    async def api_feed(request: Request, n: int = Query(12, ge=1, le=50), exclude: str = ""):
        # Anonymized random deck. ``exclude`` is a comma-separated list of cids the client
        # has already swiped this session (best-effort de-dup; the vote tables ignore true
        # repeats anyway). No acta id / location / path ever appears in the response.
        if not _feed_allow(_voter_ip(request)):
            raise HTTPException(status_code=429, detail="slow down")
        skip = {c for c in exclude.split(",") if c}
        return {"items": _feed_payload(n, skip)}

    def _acta_deck_payload(exclude_docs: set[str] | None = None) -> list[dict]:
        """All candidate crops of ONE randomly-picked acta, shuffled and anonymized.

        Powers the grid-voting page: the contributor sees every casilla of a single mesa
        at once (so the whole acta gets reviewed in one pass) WITHOUT learning which mesa
        it is — the response carries only opaque cids + image urls, never the document id,
        location or candidate names. Picks the acta the same cheap random-PK way the feed
        samples crops (a few index seeks, snappy even on a cold page cache); see
        [[_feed_payload]]. ``exclude_docs`` is unused server-side today but lets a caller
        steer away from a just-served acta. Registers every cid so /c/{cid} and
        /api/vote-batch can resolve them.
        """
        exclude_docs = exclude_docs or set()
        with conn() as db:
            maxid = db.execute("SELECT max(id) AS m FROM vote_fields").fetchone()["m"] or 0
            document_id = None
            attempts = 0
            while document_id is None and maxid and attempts < 12:
                attempts += 1
                ids = [random.randint(1, maxid) for _ in range(8)]
                placeholders = ",".join("?" * len(ids))
                rows = db.execute(
                    f"SELECT document_id FROM vote_fields WHERE id IN ({placeholders}) "
                    f"AND row_type='candidate' AND raw_crop_path IS NOT NULL",
                    ids,
                ).fetchall()
                for r in rows:
                    if r["document_id"] not in exclude_docs:
                        document_id = r["document_id"]
                        break
            if document_id is None:
                return []
            frows = db.execute(
                "SELECT page_number, row_number, section, raw_crop_path FROM vote_fields "
                "WHERE document_id=? AND row_type='candidate' AND raw_crop_path IS NOT NULL "
                "ORDER BY page_number, row_number",
                (document_id,),
            ).fetchall()
        reg: list[tuple[str, str, str, str]] = []
        items: list[dict] = []
        for fr in frows:
            fkey = field_key_of(document_id, fr["page_number"], fr["row_number"], fr["section"])
            cid = crop_id(poll_cfg.form_token_secret, fkey)
            reg.append((cid, fkey, fr["raw_crop_path"], document_id))
            items.append({"cid": cid, "img_url": crop_cdn_url(fr["raw_crop_path"], config.CDN_BASE_URL) or f"/c/{cid}"})
        community.register_cids(reg)
        random.shuffle(items)  # break ballot order so position can't hint the candidate
        return items

    @app.get("/api/acta-deck")
    async def api_acta_deck(request: Request):
        # One random anonymized acta as a grid of casillas. No acta id / location ever leaks.
        if not _feed_allow(_voter_ip(request)):
            raise HTTPException(status_code=429, detail="slow down")
        return {"items": _acta_deck_payload()}

    def _hot_crops_payload() -> list[dict]:
        """Resolve the hot-crop ranking into public billboard cards.

        Unlike the swipe deck, the billboard is intentionally *de-anonymized*: the published
        tally is public and a card links to its acta so people can investigate. Only the
        voting act stays anonymous (no per-voter identity is ever exposed). Each item carries
        the crop image, its acta ``document_id`` + a location label, and the public tallies.
        """
        # Promotion floor: a crop only headlines the public billboard once >= MIN_PROMOTE_VOTERS
        # distinct voters flagged it (post /64-bucketing, ~distinct networks) — one identity can't
        # manufacture a "hot" crop. Over-fetch then filter+truncate so the floor doesn't under-fill.
        floor = config.MIN_PROMOTE_VOTERS
        if floor > 1:
            hot = [h for h in community.hot_crops(HOTLIST_SIZE * 4)
                   if h["strange"] >= floor][:HOTLIST_SIZE]
        else:
            hot = community.hot_crops(HOTLIST_SIZE)
        if not hot:
            return []
        keys = [h["field_key"] for h in hot]
        with conn() as db:
            meta_by_key: dict[str, dict] = {}
            for fkey in keys:
                looked = lookup_candidate_appeal(db, fkey)
                if not looked:
                    continue
                doc_id = fkey.rsplit(":", 3)[0]
                doc = db.execute(
                    "SELECT department_name, department_code, municipality_name, mesa "
                    "FROM documents WHERE document_id=?",
                    (doc_id,),
                ).fetchone()
                loc = doc_id
                if doc:
                    loc = " · ".join(x for x in (
                        doc["department_name"] or doc["department_code"],
                        doc["municipality_name"],
                        f"mesa {doc['mesa']}" if doc["mesa"] else None,
                    ) if x) or doc_id
                meta_by_key[fkey] = {"crop_rel": looked[0], "document_id": doc_id, "loc": loc}
        out = []
        for h in hot:
            m = meta_by_key.get(h["field_key"])
            if not m:
                continue
            # cid_for registers the cid so /c/{cid} resolves the image without exposing the path.
            cid = cid_for(h["field_key"], m["crop_rel"], m["document_id"])
            out.append({
                "cid": cid,
                "img_url": crop_cdn_url(m["crop_rel"], config.CDN_BASE_URL) or f"/c/{cid}",
                "document_id": m["document_id"],
                "loc": m["loc"],
                "good": h["good"],
                "strange": h["strange"],
            })
        return out

    def _acta_card(db, doc_id: str, reporters: int, doc_row=None) -> dict | None:
        """One billboard-style card for an acta: a representative thumbnail (its most-flagged
        casilla), a location label, and a crowd tally (distinct reporters + how many casillas were
        flagged). Shared by the public billboard (``_hot_actas_page``) and the
        ``/buscar?review=1`` list so both render the identical tile. Pass ``doc_row``
        to reuse a row already fetched (the review list already has it) and skip the geo lookup.
        """
        if doc_row is None:
            doc_row = db.execute(
                "SELECT department_name, department_code, municipality_name, municipality_code, "
                "zone, puesto, mesa, place_name FROM documents WHERE document_id=?",
                (doc_id,),
            ).fetchone()
        if not doc_row:
            return None
        doc = enrich_doc_names(doc_row, geo)  # fill missing names from the lookup
        frows = db.execute(
            "SELECT page_number, row_number, section, raw_crop_path FROM vote_fields "
            "WHERE document_id=? AND row_type='candidate' AND raw_crop_path IS NOT NULL "
            "ORDER BY page_number, row_number",
            (doc_id,),
        ).fetchall()
        if not frows:
            return None
        fkeys = [field_key_of(doc_id, r["page_number"], r["row_number"], r["section"]) for r in frows]
        counts = counts_among_cached(fkeys)
        # Thumbnail = the acta's most-flagged casilla; tally = how many casillas got flagged.
        best = max(range(len(frows)), key=lambda i: counts[fkeys[i]]["strange"])
        flagged = sum(1 for k in fkeys if counts[k]["strange"] > 0)
        # Graded "extraña" tone (0..1): how far the acta as a whole leans negative (extrañas vs
        # "se ve bien"). Same signal as the per-crop ring on the acta page, rolled up across all
        # its casillas, so every card reddens consistently with the strength of the crowd's doubt.
        s_tot = sum(counts[k]["strange"] for k in fkeys)
        g_tot = sum(counts[k]["good"] for k in fkeys)
        flag_level = _flag_level(s_tot, g_tot)
        loc = " · ".join(x for x in (
            doc["department_name"] or doc["department_code"],
            doc["municipality_name"],
            f"mesa {doc['mesa']}" if doc["mesa"] else None,
        ) if x) or doc_id
        crop_rel = frows[best]["raw_crop_path"]
        img = crop_cdn_url(crop_rel, config.CDN_BASE_URL) or ("/crop?path=" + quote(crop_rel))
        return {
            "document_id": doc_id,
            "img_url": img,
            "loc": loc,
            "place_name": doc["place_name"],
            "reporters": reporters,
            "flagged": flagged,
            "n_candidates": len(frows),
            "flag_level": flag_level,
        }

    def _acta_flag_levels(db, doc_ids: list[str]) -> dict[str, float]:
        """Acta-level "extraña" tone (0..1) for a set of actas, batched. One vote_fields read for
        all their candidate casillas (local, indexed) + one ``counts_among`` round-trip, then each
        acta's strange-vs-"se ve bien" margin is rolled into the same graded signal the billboard
        card and the acta page use. The caller should pass only actas that have votes — the unvoted
        tail is 0 and needs no lookup."""
        if not doc_ids:
            return {}
        ph = ",".join("?" for _ in doc_ids)
        rows = db.execute(
            f"SELECT document_id, page_number, row_number, section FROM vote_fields "
            f"WHERE row_type='candidate' AND raw_crop_path IS NOT NULL "
            f"AND document_id IN ({ph})",
            doc_ids,
        ).fetchall()
        fk_by_doc: dict[str, list[str]] = {}
        all_fkeys: list[str] = []
        for r in rows:
            fk = field_key_of(r["document_id"], r["page_number"], r["row_number"], r["section"])
            fk_by_doc.setdefault(r["document_id"], []).append(fk)
            all_fkeys.append(fk)
        counts = counts_among_cached(all_fkeys)
        out: dict[str, float] = {}
        for doc_id, fks in fk_by_doc.items():
            s = sum(counts[k]["strange"] for k in fks)
            g = sum(counts[k]["good"] for k in fks)
            out[doc_id] = _flag_level(s, g)
        return out

    def _hot_actas_page(page: int) -> dict:
        """Paginated "most-reported-as-extraña" billboard for /reportes. Ranks EVERY acta the crowd
        has flagged by how many distinct people reported it, then builds one ``_acta_card`` per acta
        on the requested page. Only a page's worth of cards is built (each costs a vote_fields +
        counts_among round-trip), and the ranked list comes from the cached popularity dict."""
        pop = _agg_cached("popularity", community.acta_popularity)
        # Promotion floor: only rank actas reported by >= MIN_PROMOTE_VOTERS distinct voters, so a
        # single identity can't push an acta onto the public "most reported" board (see _hot_crops_payload).
        floor = config.MIN_PROMOTE_VOTERS
        ranked = sorted(
            ((d, r) for d, r in pop.items() if r >= floor), key=lambda kv: kv[1], reverse=True
        ) if pop else []
        total = len(ranked)
        pages = max(1, math.ceil(total / REPORTES_PER_PAGE))
        page = max(1, min(page, pages))
        window = ranked[(page - 1) * REPORTES_PER_PAGE: page * REPORTES_PER_PAGE]
        cards: list[dict] = []
        if window:
            with conn() as db:
                for doc_id, reporters in window:
                    card = _acta_card(db, doc_id, reporters)
                    if card:
                        cards.append(card)
        return {"cards": cards, "page": page, "pages": pages, "total": total}

    @app.get("/api/billboard")
    async def api_billboard():
        # Public leaderboard of the most-reported crops (tallies + acta link shown).
        return {"items": _hot_crops_payload()}

    @app.get("/api/total-reviews")
    async def api_total_reviews():
        # Live community total for the app-bar counter. Lets an active reviewer on /votar (who
        # never reloads the page) watch the crowd's number climb. Served straight from the same
        # 300s aggregate cache as the page render, so polling clients are cheap.
        return {"total": _total_reviews()}

    @app.get("/api/acta-crops")
    async def api_acta_crops(cid: str):
        # Scroll-down context: the OTHER candidate crops from the same acta as ``cid``,
        # shuffled and still anonymized (no acta id / location / candidate names leak).
        row = resolve_cid_cached(cid)
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        document_id = row["document_id"]
        with conn() as db:
            frows = db.execute(
                """
                SELECT page_number, row_number, section, raw_crop_path
                FROM vote_fields
                WHERE document_id=? AND row_type='candidate' AND raw_crop_path IS NOT NULL
                """,
                (document_id,),
            ).fetchall()
        siblings = []
        reg: list[tuple[str, str, str, str]] = []
        for fr in frows:
            fkey = field_key_of(document_id, fr["page_number"], fr["row_number"], fr["section"])
            scid = crop_id(poll_cfg.form_token_secret, fkey)
            reg.append((scid, fkey, fr["raw_crop_path"], document_id))
            siblings.append({"field_key": fkey, "cid": scid, "crop": fr["raw_crop_path"]})
        community.register_cids(reg)
        counts = counts_among_cached([s["field_key"] for s in siblings])
        items = [
            {"cid": s["cid"], "img_url": crop_cdn_url(s["crop"], config.CDN_BASE_URL) or f"/c/{s['cid']}",
             "good": counts[s["field_key"]]["good"], "strange": counts[s["field_key"]]["strange"]}
            for s in siblings
        ]
        random.shuffle(items)
        return {"items": items}

    @app.post("/api/session")
    async def api_session(request: Request, payload: dict = Body(...)):
        """Exchange a solved Turnstile challenge for a signed form token.

        This is the bot gate for the swipe feed: when Turnstile is enabled the page ships
        WITHOUT a usable form token, so a client must POST a valid ``turnstile_token`` here to
        receive one (then votes carry it as before). One solve per session covers the whole
        deck — no per-swipe challenge. When Turnstile is off this still mints a token, so the
        client can use the same flow uniformly.
        """
        sid = request.cookies.get("sid") or uuid.uuid4().hex
        new_sid = "sid" not in request.cookies
        if not _origin_allowed(request):
            return _flag_response({"ok": False, "error": "invalid_request"}, 403, sid, new_sid)
        if poll_cfg.turnstile_enabled and not await asyncio.to_thread(
            verify_turnstile, poll_cfg.turnstile_secret,
            payload.get("turnstile_token"), _client_ip(request),
        ):
            return _flag_response({"ok": False, "error": "challenge_failed"}, 403, sid, new_sid)
        form_token = (
            issue_form_token(poll_cfg.form_token_secret, sid, _voter_ip(request))
            if poll_cfg.form_token_secret else ""
        )
        return _flag_response({"ok": True, "form_token": form_token}, 200, sid, new_sid)

    @app.post("/api/vote")
    async def api_vote(request: Request, payload: dict = Body(...)):
        """Cast one anonymized vote on a crop: ``{cid, value: good|strange, form_token, website}``.

        Best-effort dedup (one identity per crop per direction, daily IP hash); a duplicate
        is a silent no-op that still returns 200 with the current public tallies. Never an
        error on a repeat — the feed just advances.
        """
        cid = str(payload.get("cid", ""))
        value = str(payload.get("value", ""))
        if not cid or value not in ("good", "strange"):
            raise HTTPException(status_code=400, detail="cid and value (good|strange) required")

        sid = request.cookies.get("sid") or uuid.uuid4().hex
        new_sid = "sid" not in request.cookies

        # Reject cross-site browser vote casting (see _origin_allowed). Cheap, before any DB work.
        if not _origin_allowed(request):
            return _flag_response({"ok": False, "error": "invalid_request"}, 403, sid, new_sid)
        token = voter_token(poll_cfg.voter_salt, _voter_ip(request))

        # The store calls below are blocking boto3 (Aurora Data API) / SQS round-trips. In an
        # async handler they must run in a thread or they stall the event loop, serializing the
        # whole machine to ~1 vote in flight. asyncio.to_thread lets one worker carry many
        # concurrent votes (both backends are thread-safe: boto3 clients are; the SQLite store
        # uses check_same_thread=False + a lock).
        if not await asyncio.to_thread(
            community.allow, token, poll_cfg.rate_refill_per_min, poll_cfg.rate_bucket
        ):
            return _flag_response({"ok": False, "error": "rate_limited"}, 429, sid, new_sid)
        bot = bot_check(payload, sid, _voter_ip(request), poll_cfg)
        if bot == "honeypot":
            return _flag_response({"ok": True}, 200, sid, new_sid)  # shadow-drop the bot
        if bot:
            return _flag_response({"ok": False, "error": "invalid_request"}, 403, sid, new_sid)
        # No per-vote Turnstile check: Turnstile is a SESSION gate. The client solves it once and
        # exchanges it at /api/session for the signed form token, which bot_check verifies above.
        # Votes carry only that form token (single-use ~300s Turnstile tokens wouldn't survive a
        # whole deck), so re-verifying a Turnstile token here rejected every vote when it was on.

        row = await asyncio.to_thread(resolve_cid_cached, cid)  # cache hit => no Data API call
        if not row:
            raise HTTPException(status_code=404, detail="unknown crop")
        field_key = row["field_key"]
        if vote_publisher is not None:
            # Durable path: enqueue (never lost) and return an OPTIMISTIC tally
            # (current + 1 for the voted direction), reconciled once the worker
            # commits. A duplicate vote is dedup'd downstream; the count just shows
            # +1 too high until the next read. Acceptable for crowd voting.
            tally = (await asyncio.to_thread(community.counts_among, [field_key]))[field_key]
            try:
                await asyncio.to_thread(vote_publisher.publish, field_key, token, value)
            except Exception as exc:  # noqa: BLE001 — page, then surface the failure to the client
                alerts.notify("vote-publish", f"SQS vote enqueue failed: {exc}")
                raise
            tally[value] += 1
        else:
            # Synchronous path (SQLite local/tests / single-machine fallback).
            if value == "strange":
                await asyncio.to_thread(community.record_flag, field_key, token)
            else:
                await asyncio.to_thread(community.record_appeal, field_key, token)
            tally = (await asyncio.to_thread(community.counts_among, [field_key]))[field_key]
        return _flag_response(
            {"ok": True, "good": tally["good"], "strange": tally["strange"]}, 200, sid, new_sid
        )

    @app.post("/api/vote-batch")
    async def api_vote_batch(request: Request, payload: dict = Body(...)):
        """Cast a whole acta's worth of votes at once: ``{strange:[cid...], good:[cid...]}``.

        The grid-voting page (/votar) shows every casilla of one anonymized mesa; the
        contributor marks the ones that look altered and sends. Marked cids become 'strange'
        flags, the rest 'good' (appeal) votes. Same anti-abuse path as the single /api/vote
        (origin, honeypot/form-token, optional Turnstile), but the rate limiter is charged
        ONCE for the whole submit — a normal full acta is ~10-20 crops and must not exhaust
        the token bucket. Duplicate votes are deduped downstream; a repeat submit is harmless.
        """
        strange = payload.get("strange") or []
        good = payload.get("good") or []
        if not isinstance(strange, list) or not isinstance(good, list):
            raise HTTPException(status_code=400, detail="strange and good must be lists of cids")
        # Bound the batch so one submit can't enqueue an unbounded amount of work.
        strange = [str(c) for c in strange][:80]
        good = [str(c) for c in good][:80]

        sid = request.cookies.get("sid") or uuid.uuid4().hex
        new_sid = "sid" not in request.cookies

        if not _origin_allowed(request):
            return _flag_response({"ok": False, "error": "invalid_request"}, 403, sid, new_sid)
        token = voter_token(poll_cfg.voter_salt, _voter_ip(request))
        # Charge the limiter proportionally: a normal full-acta batch (~10-20 crops) costs ~1
        # token, but an oversized submit is charged ceil(n / BATCH_VOTES_PER_TOKEN) so one identity
        # can't enqueue an outsized amount of work per submit. Consuming a token on a rejected
        # oversized batch is intended (it's rate limiting).
        charges = max(1, math.ceil((len(strange) + len(good)) / config.BATCH_VOTES_PER_TOKEN))
        for _ in range(charges):
            if not await asyncio.to_thread(
                community.allow, token, poll_cfg.rate_refill_per_min, poll_cfg.rate_bucket
            ):
                return _flag_response({"ok": False, "error": "rate_limited"}, 429, sid, new_sid)
        bot = bot_check(payload, sid, _voter_ip(request), poll_cfg)
        if bot == "honeypot":
            return _flag_response({"ok": True, "strange": 0, "good": 0}, 200, sid, new_sid)
        if bot:
            return _flag_response({"ok": False, "error": "invalid_request"}, 403, sid, new_sid)
        # No per-vote Turnstile check: Turnstile is a SESSION gate. The client solves it once and
        # exchanges it at /api/session for the signed form token, which bot_check verifies above.
        # Votes carry only that form token (single-use ~300s Turnstile tokens wouldn't survive a
        # whole deck), so re-verifying a Turnstile token here rejected every vote when it was on.

        async def record(cid: str, value: str) -> bool:
            row = await asyncio.to_thread(resolve_cid_cached, cid)
            if not row:
                return False
            field_key = row["field_key"]
            if vote_publisher is not None:
                try:
                    await asyncio.to_thread(vote_publisher.publish, field_key, token, value)
                except Exception as exc:  # noqa: BLE001 — page, then surface to the client
                    alerts.notify("vote-publish", f"SQS vote enqueue failed: {exc}")
                    raise
            elif value == "strange":
                await asyncio.to_thread(community.record_flag, field_key, token)
            else:
                await asyncio.to_thread(community.record_appeal, field_key, token)
            return True

        n_strange = sum([await record(c, "strange") for c in strange])
        n_good = sum([await record(c, "good") for c in good])
        return _flag_response(
            {"ok": True, "strange": n_strange, "good": n_good}, 200, sid, new_sid
        )

    return app


def bot_check(payload: dict, sid: str, ip: str, poll: PollConfig) -> str:
    """In-app bot screen. Returns '' to proceed, 'honeypot' (silently drop a bot), or
    'bad_token' (forged/missing/too-fast/wrong-IP submit). Skipped when no form-token secret.
    ``ip`` binds the token to the client IP so a solved token can't be replayed from elsewhere."""
    if str(payload.get("website", "")).strip():
        return "honeypot"  # a hidden field only bots fill
    if poll.form_token_secret and not verify_form_token(
        poll.form_token_secret, sid, payload.get("form_token"),
        poll.form_min_seconds, poll.form_max_seconds, ip=ip,
    ):
        return "bad_token"
    return ""


def _origin_allowed(request: Request) -> bool:
    """Block cross-site (CSRF-style) vote casting from a browser.

    Policy: only reject when an ``Origin`` header is present AND not in the allowlist — the
    unambiguous cross-site-browser case. A same-origin fetch sends a matching Origin; a
    non-browser client (no Origin) is left to the rate-limit/Turnstile controls rather than
    blocked here, so we never lock out legitimate traffic. No allowlist configured => allow
    (local dev / tests). CORS can't do this — it governs response *reads*, not the request.
    """
    allowed = config.ALLOWED_ORIGINS
    if not allowed:
        return True
    origin = request.headers.get("origin")
    if not origin:
        return True
    return origin.rstrip("/") in allowed


def _flag_response(body: dict, status: int, sid: str, set_sid: bool) -> JSONResponse:
    response = JSONResponse(body, status_code=status)
    if set_sid:
        response.set_cookie("sid", sid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return response
