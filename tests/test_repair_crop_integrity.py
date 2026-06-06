"""Crop-integrity repair: backfill dropped crop paths + dedupe double-ingested rows.

Mirrors the two real bugs in the recover9 snapshot — candidate rows whose raw_crop_path was
dropped (crop still on disk) and a batch whose rows were inserted twice — on a tiny fixture DB.
"""
from pathlib import Path

from PIL import Image

from e14detector.schemas import DocumentMetadata, VoteField
from e14detector.storage import DetectorStore
from e14detector.webapp import ensure_crop_paths
from scripts.repair_crop_integrity import main as repair_main


def _png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color=(255, 255, 255)).save(path)


def _cand(did: str, row: int, raw: str | None) -> VoteField:
    return VoteField(
        document_id=did, page_number=1, row_type="candidate", row_number=row,
        candidate_number=row, candidate_name=f"C{row}", section="votacion", raw_crop_path=raw,
    )


def _build(tmp_path: Path) -> tuple[Path, Path]:
    """A DB with one under-count acta (one recoverable + one genuinely-absent crop) and one
    double-ingested acta. Returns (output_dir, db_path)."""
    output_dir = tmp_path / "out"
    crops = output_dir / "crops"
    db = output_dir / "results" / "results.sqlite"
    store = DetectorStore(db)

    # under: rows 1,2,5 have crops; row3 lost its path but the file exists (recoverable);
    # row4 lost its path AND has no file on disk (a genuine gap -> reported, not backfilled).
    store.upsert_document(DocumentMetadata(document_id="under", source_path="under.pdf"))
    for r in (1, 2, 5):
        name = crops / f"under_p1_row{r}_candidate_field.png"
        _png(name)
        store.insert_vote_field(_cand("under", r, str(name)))
    _png(crops / "under_p1_row3_candidate_field.png")  # file exists, DB path NULL
    store.insert_vote_field(_cand("under", 3, None))
    store.insert_vote_field(_cand("under", 4, None))   # no file on disk

    # dup: a single candidate row inserted twice (identical path) -> one surplus row.
    store.upsert_document(DocumentMetadata(document_id="dup", source_path="dup.pdf"))
    dname = crops / "dup_p1_row1_candidate_field.png"
    _png(dname)
    store.insert_vote_field(_cand("dup", 1, str(dname)))
    store.insert_vote_field(_cand("dup", 1, str(dname)))

    store.commit()
    store.close()
    return output_dir, db


def _conn(db: Path):
    import sqlite3
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def test_repair_backfills_dedupes_and_reports_genuine_gap(tmp_path: Path) -> None:
    _, db = _build(tmp_path)
    crops_dir = str((tmp_path / "out" / "crops"))

    # row4 has no crop on disk -> exit 3 (a genuine gap, surfacing the deferred-UI need).
    assert repair_main(["--db", str(db), "--apply", "--quiet"]) == 3

    con = _conn(db)
    try:
        # row3 recovered to its deterministic path; row4 still NULL (no file to point at).
        r3 = con.execute("SELECT raw_crop_path FROM vote_fields WHERE document_id='under' AND row_number=3").fetchone()[0]
        assert r3 == f"{crops_dir}/under_p1_row3_candidate_field.png"
        r4 = con.execute("SELECT raw_crop_path FROM vote_fields WHERE document_id='under' AND row_number=4").fetchone()[0]
        assert r4 is None

        # dup collapsed to exactly one row, keeping the smallest id.
        dup_rows = con.execute("SELECT id FROM vote_fields WHERE document_id='dup' AND row_number=1").fetchall()
        assert len(dup_rows) == 1

        # n_candidates recomputed: under has 4 crop-backed rows (1,2,3,5), dup has 1.
        ncs = dict(con.execute("SELECT document_id, n_candidates FROM documents"))
        assert ncs == {"under": 4, "dup": 1}
    finally:
        con.close()

    # Idempotent: a second apply changes nothing (but still exit 3 for the persistent gap).
    assert repair_main(["--db", str(db), "--apply", "--quiet"]) == 3
    con = _conn(db)
    try:
        assert con.execute("SELECT count(*) FROM vote_fields WHERE document_id='dup'").fetchone()[0] == 1
    finally:
        con.close()


def test_serve_time_guard_heals_recoverable_only_and_keeps_dupes(tmp_path: Path) -> None:
    _, db = _build(tmp_path)
    crops_dir = str((tmp_path / "out" / "crops"))

    # The guard heals row3 (crop on disk) but must LEAVE row4 NULL (no file — a genuine gap that
    # must stay filtered out, not surface as a broken card). It must not delete the duplicate.
    assert ensure_crop_paths(db) is True

    con = _conn(db)
    try:
        r3 = con.execute("SELECT raw_crop_path FROM vote_fields WHERE document_id='under' AND row_number=3").fetchone()[0]
        assert r3 == f"{crops_dir}/under_p1_row3_candidate_field.png"
        r4 = con.execute("SELECT raw_crop_path FROM vote_fields WHERE document_id='under' AND row_number=4").fetchone()[0]
        assert r4 is None  # genuine gap left alone
        # n_candidates counts the 4 crop-backed rows (1,2,3,5); the duplicate row is left intact.
        assert con.execute("SELECT n_candidates FROM documents WHERE document_id='under'").fetchone()[0] == 4
        assert con.execute("SELECT count(*) FROM vote_fields WHERE document_id='dup'").fetchone()[0] == 2
    finally:
        con.close()

    # Idempotent: re-run heals nothing new and still returns True.
    assert ensure_crop_paths(db) is True
