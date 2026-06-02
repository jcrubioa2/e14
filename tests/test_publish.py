from pathlib import Path

from PIL import Image

from e14detector.publish import crop_upload_plan, publish_crops
from e14detector.schemas import DocumentMetadata, VoteField
from e14detector.storage import DetectorStore


class _FakeS3:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []

    def upload_file(self, local, bucket, key, ExtraArgs=None):  # noqa: N803 (boto3 signature)
        self.uploaded.append((bucket, key))


def _seed(tmp_path: Path) -> Path:
    output_dir = tmp_path / "out"
    db = output_dir / "results" / "results.sqlite"
    crops = output_dir / "crops"
    crops.mkdir(parents=True, exist_ok=True)
    store = DetectorStore(db)
    for i in range(3):
        cp = crops / f"c{i}_candidate_field.png"
        Image.new("RGB", (8, 8), (255, 255, 255)).save(cp)
        store.upsert_document(DocumentMetadata(document_id=f"doc-{i}", source_path=f"doc-{i}.pdf"))
        store.insert_vote_field(VoteField(
            document_id=f"doc-{i}", page_number=1, row_type="candidate", row_number=1,
            candidate_name="A", raw_crop_path=str(cp),
        ))
    # A summary row must NOT be published.
    store.insert_vote_field(VoteField(
        document_id="doc-0", page_number=1, row_type="summary", row_number=14,
        section="total", raw_crop_path=str(crops / "c0_candidate_field.png"),
    ))
    store.commit()
    store.close()
    return output_dir


def test_plan_keys_are_crops_suffix_and_candidate_only(tmp_path: Path) -> None:
    output_dir = _seed(tmp_path)
    plan = crop_upload_plan(output_dir)
    keys = sorted(k for _, k in plan)
    assert keys == ["crops/c0_candidate_field.png", "crops/c1_candidate_field.png",
                    "crops/c2_candidate_field.png"]
    assert all(local.exists() for local, _ in plan)


def test_publish_is_incremental_via_manifest(tmp_path: Path) -> None:
    output_dir = _seed(tmp_path)
    s3 = _FakeS3()

    first = publish_crops(output_dir, bucket="b", client=s3, verbose=False)
    assert first == {"uploaded": 3, "skipped": 0, "failed": 0}
    assert {k for _, k in s3.uploaded} == {
        "crops/c0_candidate_field.png", "crops/c1_candidate_field.png", "crops/c2_candidate_field.png",
    }

    # Second run: all in the manifest -> nothing re-uploaded.
    s3b = _FakeS3()
    second = publish_crops(output_dir, bucket="b", client=s3b, verbose=False)
    assert second == {"uploaded": 0, "skipped": 3, "failed": 0}
    assert s3b.uploaded == []


def test_dry_run_uploads_nothing(tmp_path: Path) -> None:
    output_dir = _seed(tmp_path)
    s3 = _FakeS3()
    totals = publish_crops(output_dir, bucket="b", client=s3, dry_run=True, verbose=False)
    assert totals["uploaded"] == 0 and s3.uploaded == []
