"""SQLite and JSONL storage for detector output."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .schemas import DocumentMetadata, VoteField

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_sha256 TEXT,
    filename TEXT,
    department_code TEXT,
    department_name TEXT,
    municipality_code TEXT,
    municipality_name TEXT,
    zone TEXT,
    puesto TEXT,
    mesa TEXT,
    place_name TEXT,
    official_lookup_url TEXT,
    metadata_confidence REAL,
    metadata_source TEXT,
    processing_timestamp TEXT,
    -- Count of candidate rows with a crop, maintained incrementally (see insert_vote_field /
    -- clear_document_results). Lets /browse list+filter actas without joining vote_fields.
    n_candidates INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vote_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    row_type TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    candidate_number INTEGER,
    candidate_name TEXT,
    section TEXT,
    raw_crop_path TEXT,
    enhanced_crop_path TEXT,
    debug_crop_path TEXT,
    slot_1_crop_path TEXT,
    slot_2_crop_path TEXT,
    slot_3_crop_path TEXT,
    read_value TEXT,
    slot_1_class TEXT,
    slot_2_class TEXT,
    slot_3_class TEXT,
    cv_classification TEXT,
    cv_score REAL,
    placeholder_overlap_score REAL,
    digit_shape_score REAL,
    shape_anomaly_slot INTEGER,
    shape_anomaly_digit TEXT,
    comparison_crop_path TEXT,
    comparison_notes TEXT,
    vlm_classification TEXT,
    vlm_confidence REAL,
    vlm_raw_json TEXT,
    final_classification TEXT,
    final_reason TEXT,
    anomaly_tags TEXT,
    needs_human_review INTEGER
);

CREATE TABLE IF NOT EXISTS cv_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    row_number INTEGER NOT NULL,
    features_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digit_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    page_number INTEGER,
    row_number INTEGER,
    mismatch_score REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS vlm_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    image_hash TEXT,
    classification TEXT,
    confidence REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS processing_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT,
    source_path TEXT,
    error_code TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runtime_runs (
    run_id TEXT PRIMARY KEY,
    start_timestamp TEXT,
    end_timestamp TEXT,
    input_dir TEXT,
    output_dir TEXT,
    dpi INTEGER,
    workers INTEGER,
    vlm_mode TEXT,
    gpu_mode_requested TEXT,
    gpu_mode_used TEXT,
    opencv_version TEXT,
    opencl_available INTEGER,
    opencl_enabled INTEGER,
    wsl_detected INTEGER,
    python_version TEXT,
    status TEXT
);

-- The public app groups/filters vote_fields by document_id + row_type and looks crops up
-- by raw_crop_path on every request; without these it full-scans a multi-million-row table.
CREATE INDEX IF NOT EXISTS idx_vf_doc_type ON vote_fields(document_id, row_type);
CREATE INDEX IF NOT EXISTS idx_vf_crop ON vote_fields(raw_crop_path);
-- Cascading drill-down (department -> municipio -> zona -> puesto) does DISTINCT lookups.
CREATE INDEX IF NOT EXISTS idx_doc_geo ON documents(department_code, municipality_code, zone, puesto);
"""


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


class DetectorStore:
    def __init__(self, db_path: Path, jsonl_path: Path | None = None):
        self.db_path = Path(db_path)
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # WAL + a generous busy timeout so the writer tolerates transient locks
        # and query/audit tools can read while a run is in progress.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def already_processed(self, document_id: str, source_sha256: str) -> bool:
        row = self.conn.execute(
            "SELECT source_sha256 FROM documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        return bool(row and row["source_sha256"] == source_sha256)

    def clear_document_results(self, document_id: str) -> None:
        # Reset the candidate tally; insert_vote_field rebuilds it as fields are re-inserted.
        self.conn.execute("UPDATE documents SET n_candidates=0 WHERE document_id=?", (document_id,))
        self.conn.execute("DELETE FROM vote_fields WHERE document_id=?", (document_id,))
        self.conn.execute("DELETE FROM cv_features WHERE document_id=?", (document_id,))
        self.conn.execute("DELETE FROM digit_comparisons WHERE document_id=?", (document_id,))
        self.conn.execute("DELETE FROM vlm_reviews WHERE document_id=?", (document_id,))
        self.conn.execute("DELETE FROM processing_errors WHERE document_id=?", (document_id,))

    def upsert_document(self, meta: DocumentMetadata) -> None:
        data = asdict(meta)
        cols = ",".join(data)
        placeholders = ",".join("?" for _ in data)
        updates = ",".join(f"{key}=excluded.{key}" for key in data if key != "document_id")
        self.conn.execute(
            f"INSERT INTO documents ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(document_id) DO UPDATE SET {updates}",
            tuple(data.values()),
        )

    def insert_vote_field(self, field: VoteField, features: dict[str, Any] | None = None) -> None:
        data = asdict(field)
        data["slot_1_class"] = field.slot_1_class.value
        data["slot_2_class"] = field.slot_2_class.value
        data["slot_3_class"] = field.slot_3_class.value
        data["cv_classification"] = field.cv_classification.value
        data["vlm_classification"] = field.vlm_classification.value if field.vlm_classification else None
        data["vlm_raw_json"] = json.dumps(field.vlm_raw_json, ensure_ascii=False) if field.vlm_raw_json else None
        data["final_classification"] = field.final_classification.value
        data["anomaly_tags"] = json.dumps(field.anomaly_tags, ensure_ascii=False)
        data["needs_human_review"] = 1 if field.needs_human_review else 0
        cols = ",".join(data)
        placeholders = ",".join("?" for _ in data)
        self.conn.execute(
            f"INSERT INTO vote_fields ({cols}) VALUES ({placeholders})",
            tuple(data.values()),
        )
        # Keep the per-acta candidate tally current (the document row already exists: the
        # processor upserts it before inserting fields). Only candidate rows with a crop count.
        if field.row_type == "candidate" and field.raw_crop_path:
            self.conn.execute(
                "UPDATE documents SET n_candidates=n_candidates+1 WHERE document_id=?",
                (field.document_id,),
            )
        if features is not None:
            self.conn.execute(
                "INSERT INTO cv_features (document_id,page_number,row_number,features_json) VALUES (?,?,?,?)",
                (field.document_id, field.page_number, field.row_number, json.dumps(features, ensure_ascii=False)),
            )
        if self.jsonl_path:
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "vote_field", "field": field, "features": features}, default=_json_default, ensure_ascii=False) + "\n")

    def fields_needing_vlm(
        self,
        limit: int | None = None,
        candidates_only: bool = True,
        document_id: str | None = None,
        require_flag: bool = True,
        document_ids: list[str] | None = None,
    ) -> list[sqlite3.Row]:
        """Vote fields with no VLM verdict yet.

        ``candidates_only`` (default) skips ``summary`` rows (votes en blanco /
        nulos / no marcados / total), which are negligible for the manipulation
        review and would otherwise waste paid VLM calls.

        ``require_flag`` (default) keeps the CV-gated behaviour (only rows the
        heuristics marked ``needs_human_review``). Set it False to review every
        candidate regardless of CV — used by the crop-only + Gemma sample pass,
        where CV is not run. ``document_ids`` restricts the pass to a subset of
        documents (e.g. a deterministic sample).
        """
        sql = (
            "SELECT id, document_id, page_number, row_number, row_type, candidate_name, "
            "raw_crop_path, enhanced_crop_path, debug_crop_path, final_classification, "
            "slot_1_class, slot_2_class, slot_3_class "
            "FROM vote_fields "
            "WHERE vlm_classification IS NULL "
        )
        if require_flag:
            sql += "AND needs_human_review=1 "
        if candidates_only:
            sql += "AND row_type='candidate' "
        params: list[str] = []
        if document_id:
            sql += "AND document_id=? "
            params.append(document_id)
        if document_ids is not None:
            if not document_ids:
                return []
            placeholders = ",".join("?" for _ in document_ids)
            sql += f"AND document_id IN ({placeholders}) "
            params.extend(document_ids)
        sql += "ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql, params).fetchall()

    def sampled_document_ids(self, sample_rate: float) -> list[str]:
        """Deterministic ~``sample_rate`` subset of documents (stable across runs).

        Sampling by a hash of ``document_id`` means the same actas are picked every
        run, so resuming/re-running never re-bills a different slice.
        """
        if sample_rate >= 1.0:
            return [r["document_id"] for r in self.conn.execute("SELECT document_id FROM documents")]
        if sample_rate <= 0.0:
            return []
        threshold = int(sample_rate * 10000)
        keep = []
        for row in self.conn.execute("SELECT document_id FROM documents"):
            doc = row["document_id"]
            bucket = int(hashlib.sha1(doc.encode("utf-8")).hexdigest(), 16) % 10000
            if bucket < threshold:
                keep.append(doc)
        return keep

    def vlm_cache_get(self, image_hash: str) -> sqlite3.Row | None:
        """Return a prior VLM verdict for an identical crop, if any (idempotent reruns)."""
        return self.conn.execute(
            "SELECT classification, confidence, raw_json FROM vlm_reviews "
            "WHERE image_hash=? LIMIT 1",
            (image_hash,),
        ).fetchone()

    def record_vlm_review(
        self,
        field_id: int,
        document_id: str,
        image_hash: str,
        classification: str,
        confidence: float,
        read_value: str | None,
        raw_json: str,
    ) -> None:
        """Persist a VLM verdict to the cache table and onto the vote_field row."""
        self.conn.execute(
            "INSERT INTO vlm_reviews (document_id,image_hash,classification,confidence,raw_json) "
            "VALUES (?,?,?,?,?)",
            (document_id, image_hash, classification, confidence, raw_json),
        )
        self.conn.execute(
            "UPDATE vote_fields SET vlm_classification=?, vlm_confidence=?, vlm_raw_json=?, "
            "read_value=COALESCE(?, read_value) WHERE id=?",
            (classification, confidence, raw_json, read_value, field_id),
        )

    def flagged_candidate_fields(self, classes: tuple[str, ...]) -> list[sqlite3.Row]:
        """Candidate rows whose screen verdict is in ``classes`` (the confirm-tier input)."""
        ph = ",".join("?" for _ in classes)
        return self.conn.execute(
            "SELECT id, document_id, page_number, row_number, candidate_name, raw_crop_path, "
            "enhanced_crop_path, debug_crop_path, final_classification, "
            "slot_1_class, slot_2_class, slot_3_class "
            f"FROM vote_fields WHERE row_type='candidate' AND vlm_classification IN ({ph})",
            classes,
        ).fetchall()

    def set_field_classification(
        self, field_id: int, classification: str, confidence: float, raw_json: str
    ) -> None:
        """Overwrite a row's verdict WITHOUT touching the vlm_reviews cache.

        Used by the confirm tier: it calls a different model/prompt than the screen, so
        caching under the same image hash would corrupt the screen cache. The vote_field
        row is the authoritative seed state.
        """
        self.conn.execute(
            "UPDATE vote_fields SET vlm_classification=?, vlm_confidence=?, vlm_raw_json=? WHERE id=?",
            (classification, confidence, raw_json, field_id),
        )

    def candidate_crops_for_labeling(
        self,
        *,
        only_unlabeled: bool = True,
        only_flagged: bool = False,
        department: str | None = None,
        document_id: str | None = None,
    ) -> list[sqlite3.Row]:
        """Candidate crops (id + identity + crop path) to hand to a local labeling pass."""
        where = ["vf.row_type='candidate'", "vf.raw_crop_path IS NOT NULL"]
        params: list[Any] = []
        if only_flagged:
            where.append("vf.vlm_classification IN ('SUSPICIOUS_OVERLAP','DIGIT_SHAPE_ANOMALY')")
        elif only_unlabeled:
            where.append("vf.vlm_classification IS NULL")
        if document_id:
            where.append("vf.document_id=?")
            params.append(document_id)
        if department:
            where.append("(d.department_code=? OR d.department_name=?)")
            params.extend([department, department])
        clause = " AND ".join(where)
        return self.conn.execute(
            "SELECT vf.id, vf.document_id, vf.page_number, vf.row_number, vf.section, "
            "vf.candidate_number, vf.candidate_name, vf.raw_crop_path, vf.vlm_classification "
            "FROM vote_fields vf JOIN documents d ON d.document_id=vf.document_id "
            f"WHERE {clause} ORDER BY vf.document_id, vf.page_number, vf.row_number",
            params,
        ).fetchall()

    def candidate_crop_paths(self) -> list[str]:
        """Distinct raw crop paths for candidate rows (the public crops to publish)."""
        rows = self.conn.execute(
            "SELECT DISTINCT raw_crop_path FROM vote_fields "
            "WHERE row_type='candidate' AND raw_crop_path IS NOT NULL"
        ).fetchall()
        return [r["raw_crop_path"] for r in rows]

    def candidate_id_for_crop(self, raw_crop_path: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM vote_fields WHERE row_type='candidate' AND raw_crop_path=? LIMIT 1",
            (raw_crop_path,),
        ).fetchone()
        return row["id"] if row else None

    def candidate_row_for_key(
        self, document_id: str, page_number: int, row_number: int, section: str | None
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT id, raw_crop_path FROM vote_fields WHERE row_type='candidate' AND document_id=? "
            "AND page_number=? AND row_number=? AND COALESCE(section,'')=? LIMIT 1",
            (document_id, page_number, row_number, section or ""),
        ).fetchone()

    def insert_error(self, document_id: str | None, source_path: str, error_code: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO processing_errors (document_id,source_path,error_code,error_message) VALUES (?,?,?,?)",
            (document_id, source_path, error_code, message),
        )

    def commit(self) -> None:
        self.conn.commit()
