"""Dynamic FastAPI report for VLM-confirmed candidate anomalies."""
from __future__ import annotations

import asyncio
import functools
import math
import os
import random
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import config
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
from .schemas import FieldClassification
from .vote_queue import make_publisher
from .vlm.base import VisionReviewer
from .vlm.factory import build_reviewer
from .vlm.prompt import VOTE_FIELD_APPEAL_PROMPT, VOTE_FIELD_CONFIRM_PROMPT, VOTE_FIELD_SCREEN_PROMPT

STRANGE_CLASSES = (FieldClassification.SUSPICIOUS_OVERLAP, FieldClassification.DIGIT_SHAPE_ANOMALY)
# /browse paginates over ACTAS (grouped), not individual crops.
BROWSE_ACTAS_PER_PAGE = 12
HOTLIST_SIZE = 8
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
# incrementally, and dbsync.build_serving_db recomputes it into the slim serving snapshot.
_ACTA_SUMMARY_SELECT = (
    "SELECT d.document_id, d.department_code, d.department_name, "
    "d.municipality_name, d.zone, d.puesto, d.mesa, d.place_name, "
    "d.n_candidates AS n_candidates FROM documents d"
)

def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


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


def _departments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # Group by code only and take the non-null name (MAX skips NULLs): some docs carry the
    # code without a name, which would otherwise show a duplicate "code-only" option.
    return conn.execute(
        """
        SELECT department_code, MAX(department_name) AS department_name
        FROM documents
        WHERE department_code IS NOT NULL AND department_code <> ''
        GROUP BY department_code
        ORDER BY department_code
        """
    ).fetchall()


def _municipios(conn: sqlite3.Connection, department: str | None) -> list[sqlite3.Row]:
    if not department:
        return []
    return conn.execute(
        "SELECT municipality_code, MAX(municipality_name) AS municipality_name FROM documents "
        "WHERE (department_code=? OR department_name=?) "
        "AND municipality_code IS NOT NULL AND municipality_code <> '' "
        "GROUP BY municipality_code ORDER BY municipality_code",
        (department, department),
    ).fetchall()


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


def crop_key(raw_crop_path: str) -> str:
    """Object-store key for a crop: the ``crops/<file>`` suffix of its stored path.

    All candidate crops live under ``<output_dir>/crops/``, so keying on that suffix
    lets the uploader and the page agree regardless of whether the stored path is
    absolute or relative.
    """
    s = str(raw_crop_path).replace("\\", "/")
    idx = s.find("crops/")
    return s[idx:] if idx != -1 else s.lstrip("/")


def crop_cdn_url(raw_crop_path: str, cdn_base: str) -> str | None:
    """Public URL for a crop on the CDN, or None when no CDN is configured.

    None => caller falls back to the in-app /crop endpoint.
    """
    if not cdn_base:
        return None
    return f"{cdn_base}/{crop_key(raw_crop_path)}"


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


def compute_sync_progress(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Public rollout status, derived purely from the served results DB.

    Counts browsable actas (those with at least one candidate crop) against the full
    national universe, and estimates remaining time from the processing-timestamp span.
    Reads the total at call time so it can be overridden per-deployment / in tests.
    """
    now = now or datetime.now(timezone.utc)
    total = max(1, config.NATIONAL_TOTAL_ACTAS)
    row = conn.execute(
        "SELECT COUNT(DISTINCT vf.document_id) AS synced, "
        "       MIN(d.processing_timestamp) AS first_ts, "
        "       MAX(d.processing_timestamp) AS last_ts "
        "FROM vote_fields vf JOIN documents d ON d.document_id = vf.document_id "
        "WHERE vf.row_type='candidate' AND vf.raw_crop_path IS NOT NULL"
    ).fetchone()
    synced = min(row["synced"] or 0, total)
    pct = round(synced * 100 / total, 1)

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
        "pct": pct,
        "complete": synced >= total,
        "last_sync_text": last_sync_text,
        "eta_text": eta_text,
    }


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


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honoring a single proxy hop (Fly/Cloudflare)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def create_app(
    results_db: Path,
    output_dir: Path,
    community_db: Path | None = None,
    reviewer: VisionReviewer | None = None,
    poll: PollConfig | None = None,
) -> FastAPI:
    results_db = Path(results_db)
    output_dir = Path(output_dir).resolve()
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    poll_cfg = poll or PollConfig.from_config()
    community = make_store(community_db or (output_dir / "community.sqlite"))
    # Durable vote path: when SQS is configured, votes are enqueued (worker drains to
    # Postgres) instead of written synchronously. None => synchronous write (local/tests).
    vote_publisher = make_publisher()

    # cid -> {field_key, crop_rel, document_id} is an immutable, append-only mapping (a cid is
    # registered when the feed surfaces it, well before it can be voted on), so resolving it
    # through a process-local LRU drops a per-vote Aurora Data API round-trip on the hot path.
    # Bounded so memory stays flat across the full national crop set; a miss falls back to the
    # store. (Caching an unknown cid's None is fine — bogus cids stay 404.)
    @functools.lru_cache(maxsize=200_000)
    def resolve_cid_cached(cid: str):
        return community.resolve_cid(cid)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Optional: keep the served results DB in sync with the local writer via the
        # object store (national rollout). Off unless E14_DB_SYNC is set, so the pilot
        # and tests are unaffected. Failures only log — they never crash the app.
        sync_task = None
        if os.environ.get("E14_DB_SYNC", "").lower() in ("1", "true", "yes") and config.CDN_BASE_URL:
            from .dbsync import refresh_db_once

            interval = int(os.environ.get("E14_DB_SYNC_INTERVAL", "60"))

            async def _warm() -> None:
                await asyncio.to_thread(_warm_db, results_db)

            async def _pull() -> str | None:
                # Returns the new sha when a fresh snapshot was swapped in, else None.
                return await asyncio.to_thread(refresh_db_once, config.CDN_BASE_URL, results_db)

            async def _sync_loop() -> None:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        if await _pull():
                            # A new (cold) file was just swapped in — warm it so the next
                            # feed request is a memory hit, not a disk fault.
                            await _warm()
                    except Exception as exc:  # noqa: BLE001 — never let sync crash serving
                        print(f"db-sync: {exc}", flush=True)

            # Block on an initial pull ONLY if there's no DB to serve yet. If the volume
            # already has one, serve it immediately and refresh in the background — no
            # 30s startup stall (decompressing the snapshot) on every deploy/restart.
            if not results_db.exists():
                try:
                    await asyncio.wait_for(_pull(), timeout=180)
                except Exception as exc:  # noqa: BLE001
                    print(f"db-sync (initial): {exc}", flush=True)
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
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp

    app.state.results_db = results_db
    app.state.output_dir = output_dir
    app.state.community = community
    app.state.poll = poll_cfg
    # Reviewer is built lazily so importing/serving never requires a live key, and
    # tests can inject a deterministic stub.
    app.state.reviewer = reviewer
    app.state._bg_tasks: set[asyncio.Task] = set()

    def get_reviewer() -> VisionReviewer:
        # The live poll path values accuracy over speed and uses a (thinking) model, so
        # give it a generous output cap rather than the tiny screen cap.
        if app.state.reviewer is None:
            app.state.reviewer = build_reviewer(max_tokens=config.LIVE_MAX_TOKENS)
        return app.state.reviewer

    def conn() -> sqlite3.Connection:
        if not results_db.exists():
            raise HTTPException(status_code=500, detail=f"results DB not found: {results_db}")
        return _connect(results_db)

    def _obtain_crop(crop_rel: str) -> tuple[Path, bool]:
        """A local image path for the VLM: the file on the volume, or — when crops live on
        the CDN (national deploy) — a temp download from it. Returns (path, is_temp)."""
        try:
            return resolve_crop_path(crop_rel, output_dir), False
        except (FileNotFoundError, ValueError):
            pass
        url = crop_cdn_url(crop_rel, config.CDN_BASE_URL)
        if not url:
            raise FileNotFoundError(crop_rel)
        import tempfile
        import urllib.request
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            with urllib.request.urlopen(url, timeout=30) as resp, open(tmp, "wb") as out:
                out.write(resp.read())
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise FileNotFoundError(crop_rel)
        return Path(tmp), True

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
        # The public landing is the crowd-voting browser.
        return RedirectResponse("/browse", status_code=308)

    @app.get("/crop")
    async def crop(path: str):
        try:
            resolved = resolve_crop_path(path, output_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="crop not found")
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="crop path outside output_dir") from exc
        return FileResponse(resolved)

    @app.get("/admin/poll")
    async def admin_poll(request: Request, key: str = ""):
        # Operator-only: private vote counts + AI verdicts. Off unless a token is configured.
        if not config.ADMIN_TOKEN:
            raise HTTPException(status_code=404, detail="not found")
        if key != config.ADMIN_TOKEN:
            raise HTTPException(status_code=403, detail="forbidden")
        rows = community.admin_overview()
        with conn() as db:
            for r in rows:
                parsed = parse_field_key(r["field_key"])
                if not parsed:
                    continue
                doc_id, page, row, section = parsed
                meta = db.execute(
                    "SELECT d.department_name, d.municipality_name, d.mesa, vf.candidate_name "
                    "FROM documents d LEFT JOIN vote_fields vf ON vf.document_id=d.document_id "
                    "AND vf.page_number=? AND vf.row_number=? AND COALESCE(vf.section,'')=? "
                    "AND vf.row_type='candidate' WHERE d.document_id=? LIMIT 1",
                    (page, row, section, doc_id),
                ).fetchone()
                r["document_id"] = doc_id
                r["acta"] = " · ".join(x for x in (
                    meta["department_name"], meta["municipality_name"],
                    f"mesa {meta['mesa']}" if meta and meta["mesa"] else None) if meta and x) if meta else doc_id
                r["candidate"] = (meta["candidate_name"] if meta else None) or f"fila {row}"
        summary = {
            "fields": len(rows),
            "votes": sum(r["votes"] for r in rows),
            "pending": sum(r["vlm_state"] == "PENDING" for r in rows),
            "strange": sum(r["published"] for r in rows),
            "clean": sum(r["vlm_state"] == "CLEAN" for r in rows),
            "cleared": sum(r["appeal_cleared"] for r in rows),
        }
        # Pipeline health: what the served DB holds vs what the publisher last shipped.
        # A large pointer age, or a served count far below the published frontier, means
        # the publisher stalled or the reader isn't swapping (the stub-DB incident class).
        with conn() as db:
            pipeline = compute_sync_progress(db)
        from .dbsync import pointer_status
        ptr = pointer_status(config.CDN_BASE_URL) if config.CDN_BASE_URL else None
        if ptr:
            age = ptr.get("age_secs")
            pipeline["pointer_sha"] = ptr["sha"]
            pipeline["pointer_raw_mb"] = round(ptr["raw_size"] / 1e6) if ptr.get("raw_size") else None
            pipeline["pointer_gz_mb"] = round(ptr["gz_size"] / 1e6, 1) if ptr.get("gz_size") else None
            pipeline["pointer_age_min"] = round(age / 60) if age is not None else None
            # Stale if the publisher hasn't flipped the pointer in >30 min (cycles run ~10-14).
            pipeline["pointer_stale"] = age is not None and age > 30 * 60
        # Models the operator can run on a crop. "" = the live poll model. Each can be run
        # as a dry preview, or "aplicar" to overwrite the actual verdict (record=1).
        review_models = [
            ("Haiku", "anthropic/claude-haiku-4.5"),
            ("Qwen", "qwen/qwen3-vl-8b-thinking"),
            ("Gemma", "google/gemma-4-31b-it"),
        ]
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"rows": rows, "summary": summary, "key": key, "pipeline": pipeline,
             "live_model": config.OPENROUTER_MODEL, "review_models": review_models},
        )

    @app.get("/admin/review")
    async def admin_review(
        request: Request, key: str = "", field_key: str = "",
        prompt: str = "confirm", model: str = "", record: int = 0,
    ):
        # Operator-only: run a VLM read on any crop on demand (debugging). Optionally a
        # different model/prompt than the live poll, and optionally record the verdict.
        if not config.ADMIN_TOKEN:
            raise HTTPException(status_code=404, detail="not found")
        if key != config.ADMIN_TOKEN:
            raise HTTPException(status_code=403, detail="forbidden")
        with conn() as db:
            looked = lookup_candidate_appeal(db, field_key)
        if not looked:
            raise HTTPException(status_code=404, detail="unknown field")
        crop_rel, _ = looked
        prompt_text = {
            "confirm": config.CONFIRM_PROMPT or VOTE_FIELD_CONFIRM_PROMPT,
            "appeal": config.APPEAL_PROMPT or VOTE_FIELD_APPEAL_PROMPT,
            "screen": VOTE_FIELD_SCREEN_PROMPT,
        }.get(prompt, config.CONFIRM_PROMPT or VOTE_FIELD_CONFIRM_PROMPT)
        reviewer = (
            build_reviewer("openrouter", model=model, max_tokens=config.LIVE_MAX_TOKENS)
            if model else get_reviewer()
        )
        local, is_temp = _obtain_crop(crop_rel)
        try:
            result = await asyncio.to_thread(
                reviewer.review_vote_field, [str(local)], {}, prompt_text
            )
        finally:
            if is_temp:
                local.unlink(missing_ok=True)
        strange = result.classification in STRANGE_CLASSES
        recorded = False
        if record:  # force the poll verdict (e.g. re-review after a model change)
            community.record_verdict(field_key, strange, community.distinct_votes(field_key), None)
            recorded = True
        return {
            "field_key": field_key,
            "model": model or config.OPENROUTER_MODEL,
            "prompt": prompt,
            "classification": result.classification.value,
            "confidence": result.confidence,
            "strange": strange,
            "recorded": recorded,
            "raw": result.raw_json,
        }

    @app.get("/robots.txt")
    async def robots():
        body = (
            "User-agent: *\n"
            "Allow: /browse\n"
            "Allow: /acta/\n"
            "Allow: /votar\n"
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
        locs = [f"{config.SITE_URL}/votar", f"{config.SITE_URL}/browse", f"{config.SITE_URL}/browse?review=1"]
        for d in deps:
            code = d["department_code"] or d["department_name"]
            if code:
                locs.append(f"{config.SITE_URL}/browse?department={_quote(str(code))}")
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
                    for r in _municipios(db, department)
                ]
            else:
                opts = []
        return {"options": opts}

    @app.get("/browse")
    async def browse(
        request: Request,
        department: str | None = None,
        municipality: str | None = None,
        zone: str | None = None,
        puesto: str | None = None,
        q: str | None = None,
        review: int = 0,
        page: int = Query(1, ge=1),
    ):
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
            popularity = community.acta_popularity()
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
            departments = _departments(db)
            # Dependent drop-downs: each level is populated only once its parent is chosen.
            municipios = _municipios(db, department)
            zonas = _zonas(db, department, municipality)
            puestos = _puestos(db, department, municipality, zone)
            progress = compute_sync_progress(db)
        high_voted_docs = {k.rsplit(":", 3)[0] for k in community.high_voted_fields(config.HIGH_VOTE_THRESHOLD)}
        actas = [
            {
                "doc": r,
                "n_candidates": r["n_candidates"],
                "high_voted": r["document_id"] in high_voted_docs,
            }
            for r in doc_rows
        ]
        return templates.TemplateResponse(
            request,
            "browse.html",
            {
                "actas": actas,
                "progress": progress,
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
                "site_url": config.SITE_URL,
                "canonical": config.SITE_URL + ("/browse?review=1" if review else "/browse"),
                "page_title": (
                    "Actas más votadas por la comunidad — Veeduría ciudadana 2026"
                    if review else
                    "Veeduría ciudadana de las actas E-14 — elecciones Colombia 2026"
                ),
                "meta_description": (
                    "Revisa las actas E-14 de las elecciones presidenciales de Colombia 2026. "
                    "Mira los votos escritos a mano en cada mesa y vota en el feed lo que se vea "
                    "alterado. Veeduría ciudadana, abierta a todos."
                ),
            },
        )

    @app.get("/acta/{document_id}")
    async def acta_detail(request: Request, document_id: str):
        # LEVEL 2 (read-only): one acta, all candidate crops in ballot order, each showing
        # its PUBLIC community tallies. Voting happens only in the swipe feed (/votar) — this
        # page no longer casts votes, it just displays what the crowd has said.
        with conn() as db:
            doc = db.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if not doc:
                raise HTTPException(status_code=404, detail="acta no encontrada")
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
        counts = community.counts_among([c["field_key"] for c in crops])
        for c in crops:
            tally = counts.get(c["field_key"], {"good": 0, "strange": 0})
            c["good"] = tally["good"]
            c["strange"] = tally["strange"]
            # Strong crowd signal stands on its own (no model verdict involved anymore).
            c["high_voted"] = tally["strange"] >= config.HIGH_VOTE_THRESHOLD
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
        form_token = (
            issue_form_token(poll_cfg.form_token_secret, sid) if poll_cfg.form_token_secret else ""
        )
        response = templates.TemplateResponse(
            request,
            "swipe.html",
            {
                "form_token": form_token,
                "site_url": config.SITE_URL,
                "canonical": f"{config.SITE_URL}/votar",
                "page_title": "Vota las casillas — Veeduría ciudadana E-14 2026",
                "meta_description": (
                    "Mira casillas de votos escritas a mano, al azar y sin saber de qué mesa "
                    "son, y vota si se ven bien o extrañas. Veeduría ciudadana elecciones 2026."
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
                    out.append({"cid": cid, "img_url": f"/c/{cid}"})
                    if len(out) >= n:
                        break
        community.register_cids(reg)
        return out

    @app.get("/api/feed")
    async def api_feed(n: int = Query(12, ge=1, le=50), exclude: str = ""):
        # Anonymized random deck. ``exclude`` is a comma-separated list of cids the client
        # has already swiped this session (best-effort de-dup; the vote tables ignore true
        # repeats anyway). No acta id / location / path ever appears in the response.
        skip = {c for c in exclude.split(",") if c}
        return {"items": _feed_payload(n, skip)}

    def _hot_crops_payload() -> list[dict]:
        """Resolve the hot-crop ranking into anonymized billboard cards with public tallies."""
        hot = community.hot_crops(HOTLIST_SIZE)
        if not hot:
            return []
        keys = [h["field_key"] for h in hot]
        with conn() as db:
            crop_by_key: dict[str, tuple[str, str]] = {}
            for fkey in keys:
                looked = lookup_candidate_appeal(db, fkey)
                if looked:
                    doc_id = fkey.rsplit(":", 3)[0]
                    crop_by_key[fkey] = (looked[0], doc_id)
        out = []
        for h in hot:
            cr = crop_by_key.get(h["field_key"])
            if not cr:
                continue
            cid = cid_for(h["field_key"], cr[0], cr[1])
            out.append({"cid": cid, "img_url": f"/c/{cid}",
                        "good": h["good"], "strange": h["strange"]})
        return out

    @app.get("/api/billboard")
    async def api_billboard():
        # Public, anonymized leaderboard of the most-voted crops (counts shown).
        return {"items": _hot_crops_payload()}

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
            siblings.append({"field_key": fkey, "cid": scid})
        community.register_cids(reg)
        counts = community.counts_among([s["field_key"] for s in siblings])
        items = [
            {"cid": s["cid"], "img_url": f"/c/{s['cid']}",
             "good": counts[s["field_key"]]["good"], "strange": counts[s["field_key"]]["strange"]}
            for s in siblings
        ]
        random.shuffle(items)
        return {"items": items}

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
        token = voter_token(poll_cfg.voter_salt, _client_ip(request))

        # The store calls below are blocking boto3 (Aurora Data API) / SQS round-trips. In an
        # async handler they must run in a thread or they stall the event loop, serializing the
        # whole machine to ~1 vote in flight. asyncio.to_thread lets one worker carry many
        # concurrent votes (both backends are thread-safe: boto3 clients are; the SQLite store
        # uses check_same_thread=False + a lock).
        if not await asyncio.to_thread(
            community.allow, token, poll_cfg.rate_refill_per_min, poll_cfg.rate_bucket
        ):
            return _flag_response({"ok": False, "error": "rate_limited"}, 429, sid, new_sid)
        bot = bot_check(payload, sid, poll_cfg)
        if bot == "honeypot":
            return _flag_response({"ok": True}, 200, sid, new_sid)  # shadow-drop the bot
        if bot:
            return _flag_response({"ok": False, "error": "invalid_request"}, 403, sid, new_sid)
        if poll_cfg.turnstile_enabled and not verify_turnstile(
            poll_cfg.turnstile_secret, payload.get("turnstile_token"), _client_ip(request)
        ):
            return _flag_response({"ok": False, "error": "invalid_request"}, 403, sid, new_sid)

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
            await asyncio.to_thread(vote_publisher.publish, field_key, token, value)
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

    return app


def bot_check(payload: dict, sid: str, poll: PollConfig) -> str:
    """In-app bot screen. Returns '' to proceed, 'honeypot' (silently drop a bot), or
    'bad_token' (forged/missing/too-fast submit). Skipped when no form-token secret."""
    if str(payload.get("website", "")).strip():
        return "honeypot"  # a hidden field only bots fill
    if poll.form_token_secret and not verify_form_token(
        poll.form_token_secret, sid, payload.get("form_token"),
        poll.form_min_seconds, poll.form_max_seconds,
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
