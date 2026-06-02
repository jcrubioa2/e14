"""Dynamic FastAPI report for VLM-confirmed candidate anomalies."""
from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from . import config
from .community import (
    CommunityStore,
    PollConfig,
    field_key_of,
    issue_form_token,
    verify_form_token,
    voter_token,
)
from .schemas import FieldClassification
from .vlm.base import VisionReviewer
from .vlm.factory import build_reviewer
from .vlm.prompt import VOTE_FIELD_APPEAL_PROMPT, VOTE_FIELD_CONFIRM_PROMPT

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

# One acta-summary row for the /browse list (grouped per document). Shared by the
# main, "most-voted-float", and "rest" queries so their columns stay identical.
_ACTA_SUMMARY_SELECT = (
    "SELECT d.document_id, d.department_code, d.department_name, "
    "d.municipality_name, d.zone, d.puesto, d.mesa, d.place_name, "
    "COUNT(*) AS n_candidates, "
    f"SUM(CASE WHEN {_ALGO_FLAG_SQL} THEN 1 ELSE 0 END) AS n_flagged "
    "FROM documents d JOIN vote_fields vf ON vf.document_id=d.document_id"
)
_ACTA_FLAGGED_ORDER = (
    f"ORDER BY (SUM(CASE WHEN {_ALGO_FLAG_SQL} THEN 1 ELSE 0 END) > 0) DESC, "
    "d.department_code, d.document_id"
)

VISIBLE_CLASSES = ("SUSPICIOUS_OVERLAP", "DIGIT_SHAPE_ANOMALY", "UNCLEAR")
# Strong deterministic CV signals. The VLM second pass is allowed to PRUNE the
# marginal UNCLEAR band (where it reduces false positives), but it must NOT be
# able to hide a strong CV catch by returning CLEAN: the VLM is non-deterministic
# and proven unreliable on faint placeholder-overlaps, so a CV overlap/shape flag
# stays in the human queue regardless of the VLM verdict (VLM stays advisory there).
STRONG_CV_CLASSES = ("SUSPICIOUS_OVERLAP", "DIGIT_SHAPE_ANOMALY")
PAGE_SIZE = 50


def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _filters(
    department: str | None,
    anomaly_type: str | None,
    min_confidence: float,
    q: str | None,
) -> tuple[str, list[Any]]:
    # Non-vetoing union: a row qualifies if the VLM flags it (confidently enough)
    # OR the CV pass raised a strong deterministic signal. The latter clause means
    # a VLM CLEAN can never hide a CV overlap/shape catch — it can only prune the
    # marginal UNCLEAR band, which is the half of the join the VLM is reliable on.
    where = [
        "vf.row_type='candidate'",
        (
            "("
            f"(vf.vlm_classification IN ({','.join('?' for _ in VISIBLE_CLASSES)})"
            " AND COALESCE(vf.vlm_confidence, 0) >= ?)"
            f" OR vf.final_classification IN ({','.join('?' for _ in STRONG_CV_CLASSES)})"
            ")"
        ),
    ]
    params: list[Any] = [*VISIBLE_CLASSES, min_confidence, *STRONG_CV_CLASSES]
    if department:
        where.append("(d.department_code=? OR d.department_name=?)")
        params.extend([department, department])
    if anomaly_type:
        if anomaly_type not in VISIBLE_CLASSES:
            raise HTTPException(status_code=400, detail="invalid anomaly type")
        # Match the effective alert: a VLM verdict, or a strong CV verdict the VLM
        # did not override.
        where.append("(vf.vlm_classification=? OR vf.final_classification=?)")
        params.extend([anomaly_type, anomaly_type])
    if q:
        where.append("(d.document_id LIKE ? OR d.filename LIKE ? OR d.place_name LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle, needle, needle])
    return " AND ".join(where), params


def _summary(conn: sqlite3.Connection, min_confidence: float) -> dict[str, Any]:
    total_docs = conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
    total_fields = conn.execute("SELECT COUNT(*) c FROM vote_fields").fetchone()["c"]
    class_counts = {
        row["classification"] or "NULL": row["c"]
        for row in conn.execute(
            "SELECT final_classification classification, COUNT(*) c "
            "FROM vote_fields GROUP BY final_classification"
        )
    }
    vlm_counts = {
        row["classification"] or "NULL": row["c"]
        for row in conn.execute(
            "SELECT vlm_classification classification, COUNT(*) c "
            "FROM vote_fields WHERE vlm_classification IS NOT NULL "
            "GROUP BY vlm_classification"
        )
    }
    qualifying_docs = conn.execute(
        f"""
        SELECT COUNT(*) c FROM (
            SELECT d.document_id
            FROM documents d JOIN vote_fields vf ON vf.document_id=d.document_id
            WHERE vf.row_type='candidate'
              AND (
                (vf.vlm_classification IN ({','.join('?' for _ in VISIBLE_CLASSES)})
                 AND COALESCE(vf.vlm_confidence, 0) >= ?)
                OR vf.final_classification IN ({','.join('?' for _ in STRONG_CV_CLASSES)})
              )
            GROUP BY d.document_id
        )
        """,
        (*VISIBLE_CLASSES, min_confidence, *STRONG_CV_CLASSES),
    ).fetchone()["c"]
    return {
        "total_docs": total_docs,
        "qualifying_docs": qualifying_docs,
        "total_fields": total_fields,
        "class_counts": class_counts,
        "vlm_counts": vlm_counts,
    }


def _departments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT department_code, department_name
        FROM documents
        WHERE department_code IS NOT NULL OR department_name IS NOT NULL
        GROUP BY department_code, department_name
        ORDER BY department_code, department_name
        """
    ).fetchall()


def _qualifying_docs(
    conn: sqlite3.Connection,
    department: str | None,
    anomaly_type: str | None,
    min_confidence: float,
    q: str | None,
    limit: int,
    offset: int,
) -> tuple[list[sqlite3.Row], int]:
    where, params = _filters(department, anomaly_type, min_confidence, q)
    count_row = conn.execute(
        f"""
        SELECT COUNT(*) c FROM (
            SELECT d.document_id
            FROM documents d JOIN vote_fields vf ON vf.document_id=d.document_id
            WHERE {where}
            GROUP BY d.document_id
            HAVING COUNT(*) > 0
        )
        """,
        params,
    ).fetchone()
    rows = conn.execute(
        f"""
        SELECT d.*,
               COUNT(*) AS n_confirmed,
               MAX(vf.vlm_confidence) AS max_confidence,
               GROUP_CONCAT(DISTINCT vf.vlm_classification) AS anomaly_types
        FROM documents d JOIN vote_fields vf ON vf.document_id=d.document_id
        WHERE {where}
        GROUP BY d.document_id
        HAVING n_confirmed > 0
        ORDER BY n_confirmed DESC, max_confidence DESC, d.document_id
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return rows, count_row["c"]


def crop_cdn_url(raw_crop_path: str, cdn_base: str) -> str | None:
    """Public URL for a crop on the CDN, or None when no CDN is configured.

    All candidate crops live under ``<output_dir>/crops/``; the CDN key mirrors that
    suffix (``crops/<file>``), so the uploader and the page agree regardless of whether
    the stored path is absolute or relative. None => caller falls back to /crop.
    """
    if not cdn_base:
        return None
    s = str(raw_crop_path).replace("\\", "/")
    idx = s.find("crops/")
    key = s[idx:] if idx != -1 else s.lstrip("/")
    return f"{cdn_base}/{key}"


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
        f"{_ACTA_SUMMARY_SELECT} WHERE {clause} AND d.document_id IN ({placeholders}) "
        "GROUP BY d.document_id",
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
    community = CommunityStore(community_db or (output_dir / "community.sqlite"))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        community.close()

    app = FastAPI(title="Revision de posibles irregularidades E-14", lifespan=lifespan)
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

    def adjudicate(field_key: str, crop_path: Path, votes_at_call: int) -> None:
        """Blocking VLM second opinion on an upvoted crop; runs on a worker thread.

        Uses the CONFIRM prompt: the crowd already pushed toward "suspicious", so the
        model is asked to judge independently (skeptical of the report) before we publish.
        """
        try:
            result = get_reviewer().review_vote_field(
                [str(crop_path)], metadata={}, prompt_text=VOTE_FIELD_CONFIRM_PROMPT
            )
            strange = result.classification in STRANGE_CLASSES
            try:
                digest = hashlib.sha256(Path(crop_path).read_bytes()).hexdigest()
            except OSError:
                digest = None
            community.record_verdict(field_key, strange, votes_at_call, digest)
        except Exception:
            # Transient failure: roll the PENDING claim back so a later flag retries.
            community.release_pending(field_key)

    def schedule_adjudication(field_key: str, crop_path: Path, votes_at_call: int) -> None:
        task = asyncio.create_task(asyncio.to_thread(adjudicate, field_key, crop_path, votes_at_call))
        app.state._bg_tasks.add(task)
        task.add_done_callback(app.state._bg_tasks.discard)

    def adjudicate_appeal(field_key: str, crop_path: Path, votes_at_call: int) -> None:
        """Neutral-prompt re-read of a strange crop; CLEAN un-publishes it."""
        appeal_prompt = config.APPEAL_PROMPT or VOTE_FIELD_APPEAL_PROMPT
        try:
            result = get_reviewer().review_vote_field(
                [str(crop_path)], metadata={}, prompt_text=appeal_prompt
            )
            cleared = result.classification not in STRANGE_CLASSES
            try:
                digest = hashlib.sha256(Path(crop_path).read_bytes()).hexdigest()
            except OSError:
                digest = None
            community.record_appeal_verdict(field_key, cleared, votes_at_call, digest)
        except Exception:
            community.release_appeal(field_key)

    def schedule_appeal(field_key: str, crop_path: Path, votes_at_call: int) -> None:
        task = asyncio.create_task(asyncio.to_thread(adjudicate_appeal, field_key, crop_path, votes_at_call))
        app.state._bg_tasks.add(task)
        task.add_done_callback(app.state._bg_tasks.discard)

    @app.get("/")
    async def dashboard(
        request: Request,
        department: str | None = None,
        anomaly_type: str | None = None,
        min_confidence: float = Query(0.0, ge=0.0, le=1.0),
        q: str | None = None,
        page: int = Query(1, ge=1),
    ):
        with conn() as db:
            docs, total = _qualifying_docs(
                db, department, anomaly_type, min_confidence, q, PAGE_SIZE, (page - 1) * PAGE_SIZE
            )
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                {
                    "summary": _summary(db, min_confidence),
                    "departments": _departments(db),
                    "docs": docs,
                    "visible_classes": VISIBLE_CLASSES,
                    "filters": {
                        "department": department or "",
                        "anomaly_type": anomaly_type or "",
                        "min_confidence": min_confidence,
                        "q": q or "",
                    },
                    "page": page,
                    "pages": max(1, math.ceil(total / PAGE_SIZE)),
                    "total": total,
                },
            )

    @app.get("/doc/{document_id}")
    async def doc_detail(request: Request, document_id: str, min_confidence: float = Query(0.0, ge=0.0, le=1.0)):
        with conn() as db:
            doc = db.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
            if not doc:
                raise HTTPException(status_code=404, detail="document not found")
            fields = db.execute(
                """
                SELECT *
                FROM vote_fields
                WHERE document_id=?
                ORDER BY CASE row_type WHEN 'candidate' THEN 0 ELSE 1 END, page_number, row_number
                """,
                (document_id,),
            ).fetchall()
            return templates.TemplateResponse(
                request,
                "doc.html",
                {
                    "doc": doc,
                    "fields": fields,
                    "visible_classes": VISIBLE_CLASSES,
                    "min_confidence": min_confidence,
                },
            )

    @app.get("/crop")
    async def crop(path: str):
        try:
            resolved = resolve_crop_path(path, output_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="crop not found")
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="crop path outside output_dir") from exc
        return FileResponse(resolved)

    @app.get("/api/flagged")
    async def api_flagged(
        department: str | None = None,
        anomaly_type: str | None = None,
        min_confidence: float = Query(0.0, ge=0.0, le=1.0),
        q: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        with conn() as db:
            docs, total = _qualifying_docs(db, department, anomaly_type, min_confidence, q, limit, offset)
            return {"total": total, "items": [_row_dict(row) for row in docs]}

    def _published_count_by_doc() -> dict[str, int]:
        counts: dict[str, int] = {}
        for key in community.published_keys():
            doc = key.rsplit(":", 3)[0]
            counts[doc] = counts.get(doc, 0) + 1
        return counts

    def _build_hotlist(db: sqlite3.Connection, popularity: dict[str, int]) -> list[dict]:
        items: dict[str, dict] = {}
        # Gemma's seed findings (the initial "stranges" to review).
        for r in db.execute(
            f"""
            SELECT d.document_id, d.department_name, d.municipality_name, d.place_name,
                   SUM(CASE WHEN {_ALGO_FLAG_SQL} THEN 1 ELSE 0 END) AS g
            FROM documents d JOIN vote_fields vf ON vf.document_id=d.document_id
            WHERE vf.row_type='candidate'
            GROUP BY d.document_id HAVING g > 0
            ORDER BY g DESC LIMIT 50
            """
        ).fetchall():
            items[r["document_id"]] = {"doc": r, "pop": popularity.get(r["document_id"], 0), "seed": True}
        # Popular actas the crowd is flagging (even if Gemma did not seed them).
        for doc_id, votes in popularity.items():
            if doc_id in items:
                items[doc_id]["pop"] = votes
                continue
            dr = db.execute(
                "SELECT document_id, department_name, municipality_name, place_name "
                "FROM documents WHERE document_id=?",
                (doc_id,),
            ).fetchone()
            if dr:
                items[doc_id] = {"doc": dr, "pop": votes, "seed": False}
        # Rank by popularity first (traction), then Gemma seeds; hide exact counts.
        ranked = sorted(items.values(), key=lambda x: (x["pop"], x["seed"]), reverse=True)
        return ranked[:HOTLIST_SIZE]

    @app.get("/browse")
    async def browse(
        request: Request,
        department: str | None = None,
        q: str | None = None,
        page: int = Query(1, ge=1),
    ):
        # LEVEL 1: a browsable summary, one entry per ACTA (a polling table). Actas the
        # automatic pipeline flagged come first. Click through to /acta/{id} to see the
        # candidate crops and flag them. Acta identity stays visible (no anonymization).
        where = ["vf.row_type='candidate'", "vf.raw_crop_path IS NOT NULL"]
        params: list[Any] = []
        if department:
            where.append("(d.department_code=? OR d.department_name=?)")
            params.extend([department, department])
        if q:
            where.append("(d.document_id LIKE ? OR d.place_name LIKE ? OR d.municipality_name LIKE ?)")
            needle = f"%{q}%"
            params.extend([needle, needle, needle])
        clause = " AND ".join(where)
        with conn() as db:
            total_actas = db.execute(
                f"SELECT COUNT(*) c FROM ("
                f"  SELECT d.document_id FROM documents d "
                f"  JOIN vote_fields vf ON vf.document_id=d.document_id "
                f"  WHERE {clause} GROUP BY d.document_id)",
                params,
            ).fetchone()["c"]
            # Ordering: the most-voted actas float to the very top (silently — crowd
            # attention compounds), then the flagged seeds, then the rest by region.
            # The voted rows are paginated as a prefix; the "rest" query excludes them.
            popularity = community.acta_popularity()
            voted_rows, voted_ids = _voted_doc_rows(db, popularity, clause, params, VOTED_FLOAT_CAP)
            offset = (page - 1) * BROWSE_ACTAS_PER_PAGE
            head = voted_rows[offset : offset + BROWSE_ACTAS_PER_PAGE]
            rest_rows: list = []
            rest_count = BROWSE_ACTAS_PER_PAGE - len(head)
            if rest_count > 0:
                rest_clause, rest_params = clause, list(params)
                if voted_ids:
                    rest_clause += f" AND d.document_id NOT IN ({','.join('?' for _ in voted_ids)})"
                    rest_params += voted_ids
                rest_rows = db.execute(
                    f"{_ACTA_SUMMARY_SELECT} WHERE {rest_clause} GROUP BY d.document_id "
                    f"{_ACTA_FLAGGED_ORDER} LIMIT ? OFFSET ?",
                    [*rest_params, rest_count, max(0, offset - len(voted_rows))],
                ).fetchall()
            doc_rows = list(head) + list(rest_rows)
            departments = _departments(db)
            # Hotlist (page 1, unfiltered): the actas to review *now* — the ones
            # people are flagging most, backfilled with Gemma's seed findings so the
            # list is never empty. Gives them traction and shared attention. No vote
            # numbers are shown (the counter stays private); it is a ranking only.
            hotlist = []
            if page == 1 and not department and not q:
                hotlist = _build_hotlist(db, popularity)
            progress = compute_sync_progress(db)
        pub_counts = _published_count_by_doc()
        # Appeals that reversed a Gemma seed: subtract them so a cleared false positive
        # stops inflating the acta's flagged count.
        cleared_by_doc: dict[str, int] = {}
        for key in community.cleared_keys():
            d = key.rsplit(":", 3)[0]
            cleared_by_doc[d] = cleared_by_doc.get(d, 0) + 1
        actas = [
            {
                "doc": r,
                "n_candidates": r["n_candidates"],
                "n_flagged": max(0, (r["n_flagged"] or 0) - cleared_by_doc.get(r["document_id"], 0)),
                "n_published": pub_counts.get(r["document_id"], 0),
            }
            for r in doc_rows
        ]
        return templates.TemplateResponse(
            request,
            "browse.html",
            {
                "actas": actas,
                "hotlist": hotlist,
                "progress": progress,
                "departments": departments,
                "filters": {"department": department or "", "q": q or ""},
                "page": page,
                "pages": max(1, math.ceil(total_actas / BROWSE_ACTAS_PER_PAGE)),
                "total": total_actas,
            },
        )

    @app.get("/acta/{document_id}")
    async def acta_detail(request: Request, document_id: str):
        # LEVEL 2: one acta, all candidate crops in ballot order, each flaggable.
        with conn() as db:
            doc = db.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if not doc:
                raise HTTPException(status_code=404, detail="acta no encontrada")
            frows = db.execute(
                f"""
                SELECT vf.page_number, vf.row_number, vf.section, vf.candidate_number,
                       vf.candidate_name, vf.raw_crop_path,
                       CASE WHEN {_ALGO_FLAG_SQL} THEN 1 ELSE 0 END AS algo_flagged
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
                "algo_flagged": bool(fr["algo_flagged"]),
                "crop_url": crop_cdn_url(fr["raw_crop_path"], config.CDN_BASE_URL),
            })
        keys = [c["field_key"] for c in crops]
        published = community.published_among(keys)
        cleared = community.cleared_among(keys)
        for c in crops:
            c["cleared"] = c["field_key"] in cleared
            # Shown as strange = a Gemma seed or a live-published crop, UNLESS an appeal
            # already cleared it. Only such crops expose the "Se ve normal" button.
            c["published"] = c["field_key"] in published and not c["cleared"]
            c["strange"] = (c["algo_flagged"] or c["published"]) and not c["cleared"]
        flagged = any(c["strange"] for c in crops)
        # Issue a stable session id so a subsequent flag POST has an identity, and a
        # signed form token bound to it (the in-app bot check; no CAPTCHA needed).
        sid = request.cookies.get("sid") or uuid.uuid4().hex
        form_token = (
            issue_form_token(poll_cfg.form_token_secret, sid) if poll_cfg.form_token_secret else ""
        )
        response = templates.TemplateResponse(
            request,
            "acta.html",
            {
                "doc": doc,
                "crops": crops,
                "flagged": flagged,
                "form_token": form_token,
            },
        )
        if "sid" not in request.cookies:
            response.set_cookie("sid", sid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
        return response

    @app.post("/api/flag")
    async def api_flag(request: Request, payload: dict = Body(...)):
        field_key = str(payload.get("field_key", ""))
        if not field_key:
            raise HTTPException(status_code=400, detail="field_key required")

        sid = request.cookies.get("sid") or uuid.uuid4().hex
        new_sid = "sid" not in request.cookies
        token = voter_token(poll_cfg.voter_salt, _client_ip(request), sid)

        # Rate limit before any paid work or Turnstile round-trip.
        if not community.allow(token, poll_cfg.rate_refill_per_min, poll_cfg.rate_bucket):
            return _flag_response({"ok": False, "error": "rate_limited"}, 429, sid, new_sid)

        bot = bot_check(payload, sid, poll_cfg)
        if bot == "honeypot":
            return _flag_response({"ok": True}, 200, sid, new_sid)  # shadow-drop the bot
        if bot:
            return _flag_response({"ok": False, "error": "invalid_request"}, 403, sid, new_sid)

        # Validate the field exists and resolve its crop (read-only results DB).
        with conn() as db:
            looked = lookup_candidate_appeal(db, field_key)
        if not looked:
            raise HTTPException(status_code=404, detail="unknown field")
        crop_rel, is_seed = looked
        # A crop already shown as strange (Gemma seed or live-published) can't be
        # re-flagged — it's already strange. The appeal path ("Se ve normal") is what
        # applies there. Mirrors the eligibility check in /api/appeal.
        strange_now = is_seed or (field_key in community.published_among([field_key]))
        if strange_now and field_key not in community.cleared_among([field_key]):
            raise HTTPException(status_code=409, detail="field is already marked strange")
        try:
            crop_path = resolve_crop_path(crop_rel, output_dir)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="crop unavailable")

        community.record_flag(field_key, token)
        votes_at_call = community.try_claim_adjudication(
            field_key, poll_cfg.threshold, poll_cfg.rescale_step
        )
        if votes_at_call is not None:
            schedule_adjudication(field_key, crop_path, votes_at_call)

        # Never leak the count or whether a review fired — the counter is private.
        return _flag_response({"ok": True}, 200, sid, new_sid)

    @app.post("/api/appeal")
    async def api_appeal(request: Request, payload: dict = Body(...)):
        """"Se ve normal": challenge a crop currently shown as strange.

        Symmetric to /api/flag. Eligibility (the crop must currently be shown strange)
        is checked here so the crowd cannot open an appeal on an ordinary crop. Crossing
        the appeal threshold only *triggers* a neutral re-read; the model decides.
        """
        field_key = str(payload.get("field_key", ""))
        if not field_key:
            raise HTTPException(status_code=400, detail="field_key required")

        sid = request.cookies.get("sid") or uuid.uuid4().hex
        new_sid = "sid" not in request.cookies
        token = voter_token(poll_cfg.voter_salt, _client_ip(request), sid)

        if not community.allow(token, poll_cfg.rate_refill_per_min, poll_cfg.rate_bucket):
            return _flag_response({"ok": False, "error": "rate_limited"}, 429, sid, new_sid)
        bot = bot_check(payload, sid, poll_cfg)
        if bot == "honeypot":
            return _flag_response({"ok": True}, 200, sid, new_sid)
        if bot:
            return _flag_response({"ok": False, "error": "invalid_request"}, 403, sid, new_sid)

        with conn() as db:
            looked = lookup_candidate_appeal(db, field_key)
        if not looked:
            raise HTTPException(status_code=404, detail="unknown field")
        crop_rel, is_seed = looked
        # Only crops currently shown as strange (Gemma seed or live-published) are
        # appealable. Already-cleared crops are no longer strange, so they are not.
        strange_now = is_seed or (field_key in community.published_among([field_key]))
        if not strange_now or field_key in community.cleared_among([field_key]):
            raise HTTPException(status_code=409, detail="field is not marked strange")
        try:
            crop_path = resolve_crop_path(crop_rel, output_dir)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="crop unavailable")

        community.record_appeal(field_key, token)
        votes_at_call = community.try_claim_appeal(
            field_key, poll_cfg.appeal_threshold, poll_cfg.appeal_rescale_step
        )
        if votes_at_call is not None:
            schedule_appeal(field_key, crop_path, votes_at_call)
        return _flag_response({"ok": True}, 200, sid, new_sid)

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


def _flag_response(body: dict, status: int, sid: str, set_sid: bool) -> JSONResponse:
    response = JSONResponse(body, status_code=status)
    if set_sid:
        response.set_cookie("sid", sid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return response
