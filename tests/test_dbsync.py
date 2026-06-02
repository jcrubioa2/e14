import json
import sqlite3
from pathlib import Path

import pytest

from e14detector import dbsync


def _make_db(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    con.commit()
    con.close()


def _publish_to_dir(src_db: Path, cdn_dir: Path) -> str:
    """Mimic publish_db onto a local 'CDN' dir (no S3): snapshot + pointer."""
    (cdn_dir / dbsync.DB_PREFIX).mkdir(parents=True, exist_ok=True)
    snap = cdn_dir / "tmp.sqlite"
    digest = dbsync.make_snapshot(src_db, snap)
    key = f"{dbsync.DB_PREFIX}/results-{digest[:16]}.sqlite"
    snap.rename(cdn_dir / key)
    (cdn_dir / dbsync.POINTER_KEY).write_text(json.dumps({"key": key, "sha256": digest}))
    return digest


def test_snapshot_is_consistent_and_hashable(tmp_path: Path) -> None:
    src = tmp_path / "src" / "results.sqlite"
    _make_db(src, 5)
    snap = tmp_path / "snap.sqlite"
    digest = dbsync.make_snapshot(src, snap)
    assert len(digest) == 64 and snap.exists()
    con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5
    con.close()


def test_refresh_round_trip_and_idempotence(tmp_path: Path) -> None:
    src = tmp_path / "src" / "results.sqlite"
    _make_db(src, 3)
    cdn = tmp_path / "cdn"
    digest = _publish_to_dir(src, cdn)
    base = cdn.as_uri()  # file:///...

    dest = tmp_path / "served" / "results.sqlite"
    got = dbsync.refresh_db_once(base, dest, timeout=10)
    assert got == digest and dest.exists()
    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
    con.close()

    # No pointer change -> no-op.
    assert dbsync.refresh_db_once(base, dest, timeout=10) is None

    # Writer adds data, republishes -> reader installs the new snapshot atomically.
    _make_db(src, 7)  # now 10 rows
    digest2 = _publish_to_dir(src, cdn)
    assert digest2 != digest
    assert dbsync.refresh_db_once(base, dest, timeout=10) == digest2
    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 10
    con.close()


def test_refresh_rejects_corrupt_snapshot_and_keeps_served_file(tmp_path: Path) -> None:
    src = tmp_path / "src" / "results.sqlite"
    _make_db(src, 4)
    cdn = tmp_path / "cdn"
    digest = _publish_to_dir(src, cdn)
    dest = tmp_path / "served" / "results.sqlite"
    dbsync.refresh_db_once(cdn.as_uri(), dest, timeout=10)

    # Corrupt the published object but advance the pointer to a new (wrong) hash.
    bad_key = f"{dbsync.DB_PREFIX}/results-deadbeefdeadbeef.sqlite"
    (cdn / bad_key).write_bytes(b"not a sqlite file")
    (cdn / dbsync.POINTER_KEY).write_text(json.dumps({"key": bad_key, "sha256": "f" * 64}))

    with pytest.raises(ValueError):
        dbsync.refresh_db_once(cdn.as_uri(), dest, timeout=10)
    # The previously-served file is untouched and still valid.
    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 4
    con.close()
    assert not list(dest.parent.glob("*.incoming"))  # temp cleaned up


def test_publish_db_uses_content_hashed_key_and_flips_pointer(tmp_path: Path) -> None:
    src_dir = tmp_path / "out"
    _make_db(src_dir / "results" / "results.sqlite", 2)

    class _FakeS3:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def upload_file(self, local, bucket, key, ExtraArgs=None):  # noqa: N803
            self.objects[key] = Path(local).read_bytes()

        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            self.objects[Key] = Body

    s3 = _FakeS3()
    info = dbsync.publish_db(src_dir, bucket="b", client=s3, verbose=False)
    assert info["key"] == f"db/results-{info['sha256'][:16]}.sqlite"
    assert info["key"] in s3.objects
    pointer = json.loads(s3.objects[dbsync.POINTER_KEY])
    assert pointer["sha256"] == info["sha256"] and pointer["key"] == info["key"]
