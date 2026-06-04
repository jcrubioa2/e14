"""publish-reconcile: rebuild the upload manifest from the bucket (source of truth)."""
from pathlib import Path

from e14detector.publish import reconcile_manifest


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, Bucket, Prefix):
        # Honor the prefix filter so the test reflects real ListObjectsV2 behavior.
        for page in self._pages:
            yield {"Contents": [o for o in page if o["Key"].startswith(Prefix)]}


class _FakeS3:
    def __init__(self, pages):
        self._pages = pages

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _FakePaginator(self._pages)


def test_reconcile_writes_union_of_bucket_and_existing(tmp_path: Path) -> None:
    out = tmp_path
    manifest = out / "review" / "uploaded_crops.txt"
    manifest.parent.mkdir(parents=True)
    # A locally-known key not (yet) seen in the listing must survive the reconcile.
    manifest.write_text("crops/local_only.png\n", encoding="utf-8")

    pages = [
        [{"Key": "crops/a.png"}, {"Key": "crops/b.png"}, {"Key": "db/results-x.sqlite.gz"}],
        [{"Key": "crops/c.png"}],
    ]
    info = reconcile_manifest(out, bucket="b", client=_FakeS3(pages), verbose=False)

    keys = set(manifest.read_text().split())
    assert keys == {"crops/local_only.png", "crops/a.png", "crops/b.png", "crops/c.png"}
    assert "db/results-x.sqlite.gz" not in keys     # prefix filter excludes non-crops
    assert info["listed"] == 3 and info["before"] == 1 and info["after"] == 4


def test_reconcile_seeds_empty_manifest(tmp_path: Path) -> None:
    out = tmp_path
    pages = [[{"Key": "crops/a.png"}, {"Key": "crops/b.png"}]]
    info = reconcile_manifest(out, bucket="b", client=_FakeS3(pages), verbose=False)
    manifest = out / "review" / "uploaded_crops.txt"
    assert set(manifest.read_text().split()) == {"crops/a.png", "crops/b.png"}
    assert info["before"] == 0 and info["after"] == 2
