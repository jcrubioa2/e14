import json
from pathlib import Path

from PIL import Image

from e14detector.labeling import export_label_queue, import_labels
from e14detector.schemas import DocumentMetadata, VoteField
from e14detector.storage import DetectorStore


def _seed_store(tmp_path: Path) -> tuple[Path, Path]:
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crops = output_dir / "crops"
    crops.mkdir(parents=True, exist_ok=True)
    store = DetectorStore(db)
    paths = []
    for i in range(3):
        cp = crops / f"c{i}.png"
        Image.new("RGB", (24, 16), (255, 255, 255)).save(cp)
        paths.append(cp)
        store.upsert_document(DocumentMetadata(document_id=f"doc-{i}", source_path=f"doc-{i}.pdf"))
        store.insert_vote_field(VoteField(
            document_id=f"doc-{i}", page_number=1, row_type="candidate", row_number=1,
            candidate_name=f"Cand {i}", raw_crop_path=str(cp),
        ))
    store.commit()
    store.close()
    return output_dir, db


def test_export_writes_queue_and_guide(tmp_path: Path) -> None:
    output_dir, _ = _seed_store(tmp_path)
    queue_path, n = export_label_queue(output_dir)
    assert n == 3
    recs = [json.loads(l) for l in queue_path.read_text().splitlines()]
    assert {r["label"] for r in recs} == {None}
    assert all(r["field_key"] and r["path"] for r in recs)
    assert (output_dir / "review" / "LABELING.md").exists()


def test_import_seeds_dirty_and_confirms_clean(tmp_path: Path) -> None:
    output_dir, db = _seed_store(tmp_path)
    queue_path, _ = export_label_queue(output_dir)
    recs = [json.loads(l) for l in queue_path.read_text().splitlines()]

    # Label first DIRTY (by field_key), second CLEAN (by path), leave third unlabeled.
    labels = output_dir / "review" / "label_done.jsonl"
    with labels.open("w") as fh:
        fh.write(json.dumps({"field_key": recs[0]["field_key"], "label": "DIRTY"}) + "\n")
        fh.write(json.dumps({"path": recs[1]["path"], "label": "clean"}) + "\n")  # case-insensitive
        fh.write(json.dumps({"field_key": "no:such:field:x", "label": "DIRTY"}) + "\n")  # unmatched

    totals = import_labels(output_dir)
    assert totals == {"dirty": 1, "clean": 1, "skipped": 0, "unmatched": 1}

    store = DetectorStore(db)
    by_doc = {
        r["document_id"]: r["vlm_classification"]
        for r in store.conn.execute(
            "SELECT document_id, vlm_classification FROM vote_fields"
        ).fetchall()
    }
    store.close()
    assert by_doc["doc-0"] == "SUSPICIOUS_OVERLAP"  # DIRTY -> public seed (uploaded)
    assert by_doc["doc-1"] is None                  # CLEAN -> registry only, not in DB
    assert by_doc["doc-2"] is None                  # untouched
    # Both labeled crops are in the registry (so they won't be re-selected).
    reg = (output_dir / "review" / "labeled_crops.txt").read_text().split()
    assert recs[0]["path"] in reg and recs[1]["path"] in reg


def test_only_unlabeled_is_default(tmp_path: Path) -> None:
    output_dir, db = _seed_store(tmp_path)
    # Pre-label doc-0 so the default (unlabeled-only) export skips it.
    store = DetectorStore(db)
    fid = store.candidate_id_for_crop(str(output_dir / "crops" / "c0.png"))
    store.set_field_classification(fid, "CLEAN", 1.0, "{}")
    store.commit()
    store.close()

    _, n = export_label_queue(output_dir)
    assert n == 2
    _, n_all = export_label_queue(output_dir, include_labeled=True)
    assert n_all == 3


def test_clean_label_excluded_via_registry_and_batch_files_removed(tmp_path: Path) -> None:
    """A CLEAN label (no DB row) is still not re-selected, and label_done.jsonl is removed."""
    output_dir, db = _seed_store(tmp_path)
    queue_path, n = export_label_queue(output_dir)
    assert n == 3
    recs = [json.loads(l) for l in queue_path.read_text().splitlines()]
    labels = output_dir / "review" / "label_done.jsonl"
    labels.write_text("\n".join(json.dumps({"path": r["path"], "label": "CLEAN"}) for r in recs))

    totals = import_labels(output_dir)
    assert totals == {"dirty": 0, "clean": 3, "skipped": 0, "unmatched": 0}
    # Clean apply removed the transient batch files.
    assert not labels.exists() and not queue_path.exists()
    # No CLEAN verdicts written to the DB (nothing to upload).
    store = DetectorStore(db)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM vote_fields WHERE vlm_classification IS NOT NULL"
    ).fetchone()[0] == 0
    store.close()
    # Re-export now finds nothing (all three are in the registry).
    _, n2 = export_label_queue(output_dir)
    assert n2 == 0
