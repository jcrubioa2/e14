import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from e14detector import dbsync


def _make_db(path: Path, rows: int) -> None:
    """A results DB shaped like the detector's working DB: the served registry columns plus
    the fat columns/tables (cv_features, debug paths, vlm_raw_json) the serving snapshot drops."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS documents (document_id TEXT PRIMARY KEY, source_path TEXT, "
                "department_code TEXT, municipality_code TEXT, zone TEXT, puesto TEXT)")
    # Served columns + fat columns that build_serving_db must NOT copy.
    con.execute("CREATE TABLE IF NOT EXISTS vote_fields (id INTEGER PRIMARY KEY, document_id TEXT, "
                "page_number INTEGER, row_number INTEGER, row_type TEXT, section TEXT, "
                "candidate_number INTEGER, candidate_name TEXT, raw_crop_path TEXT, "
                "vlm_classification TEXT, "
                "debug_crop_path TEXT, cv_score REAL, vlm_raw_json TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS cv_features (id INTEGER PRIMARY KEY, features_json TEXT)")  # must NOT be copied
    start = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]  # cumulative, unique ids
    con.executemany("INSERT INTO documents (document_id, source_path) VALUES (?, ?)",
                    [(f"d{i}", f"d{i}.pdf") for i in range(start, start + rows)])
    con.executemany(
        "INSERT INTO vote_fields (document_id, page_number, row_number, row_type, candidate_name, "
        "raw_crop_path, vlm_classification, debug_crop_path, cv_score, vlm_raw_json) "
        "VALUES (?, 1, 1, 'candidate', ?, ?, 'CLEAN', 'crops/dbg.png', 0.5, '{\"k\":1}')",
        [(f"d{i}", f"C{i}", f"crops/c{i}.png") for i in range(start, start + rows)])
    con.execute("INSERT INTO cv_features (features_json) VALUES ('x')")
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
    assert con.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0] == 5
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
    assert con.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0] == 3
    con.close()

    # No pointer change -> no-op.
    assert dbsync.refresh_db_once(base, dest, timeout=10) is None

    # Writer adds data, republishes -> reader installs the new snapshot atomically.
    _make_db(src, 7)  # now 10 rows
    digest2 = _publish_to_dir(src, cdn)
    assert digest2 != digest
    assert dbsync.refresh_db_once(base, dest, timeout=10) == digest2
    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0] == 10
    con.close()


def test_refresh_handles_gzipped_snapshot(tmp_path: Path) -> None:
    """Reader decompresses a .gz snapshot on the fly and verifies the decompressed sha."""
    import shutil

    src = tmp_path / "src" / "results.sqlite"
    _make_db(src, 6)
    cdn = tmp_path / "cdn"
    (cdn / dbsync.DB_PREFIX).mkdir(parents=True)
    snap = cdn / "s.sqlite"
    digest = dbsync.make_snapshot(src, snap)
    key = f"{dbsync.DB_PREFIX}/results-{digest[:16]}.sqlite.gz"
    with open(snap, "rb") as f_in, gzip.open(cdn / key, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    snap.unlink()
    (cdn / dbsync.POINTER_KEY).write_text(json.dumps({"key": key, "sha256": digest}))

    dest = tmp_path / "served" / "results.sqlite"
    assert dbsync.refresh_db_once(cdn.as_uri(), dest, timeout=10) == digest
    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0] == 6
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
    assert con.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0] == 4
    con.close()
    assert not list(dest.parent.glob("*.incoming"))  # temp cleaned up


def test_publish_db_only_uploaded_publishes_the_frontier(tmp_path: Path) -> None:
    """--only-uploaded drops actas whose crops aren't all in the manifest."""
    out = tmp_path / "out"
    db = out / "results" / "results.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY, source_path TEXT)")
    con.execute(
        "CREATE TABLE vote_fields (id INTEGER PRIMARY KEY, document_id TEXT, page_number INTEGER, "
        "row_number INTEGER, row_type TEXT, section TEXT, candidate_number INTEGER, "
        "candidate_name TEXT, raw_crop_path TEXT, vlm_classification TEXT)"
    )
    for d in ("doc-0", "doc-1"):
        con.execute("INSERT INTO documents VALUES (?, ?)", (d, f"{d}.pdf"))
        con.execute(
            "INSERT INTO vote_fields (document_id, page_number, row_number, row_type, raw_crop_path) "
            "VALUES (?,1,1,?,?)",
            (d, "candidate", f"data/x/crops/{d}.png"),
        )
    con.commit()
    con.close()
    # Only doc-0's crop is uploaded.
    manifest = out / "review" / "uploaded_crops.txt"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("crops/doc-0.png\n")

    class _FakeS3:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def upload_file(self, local, bucket, key, ExtraArgs=None):  # noqa: N803
            self.objects[key] = Path(local).read_bytes()

        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            self.objects[Key] = Body

    s3 = _FakeS3()
    info = dbsync.publish_db(out, bucket="b", client=s3, only_uploaded=True, verbose=False)
    assert info is not None and info["kept"] == 1
    assert info["key"].endswith(".sqlite.gz")

    snap = tmp_path / "got.sqlite"
    snap.write_bytes(gzip.decompress(s3.objects[info["key"]]))  # stored object is gzipped
    assert dbsync._sha256(snap) == info["sha256"]  # pointer sha is of the decompressed db
    con = sqlite3.connect(snap)
    docs = {r[0] for r in con.execute("SELECT document_id FROM documents")}
    con.close()
    assert docs == {"doc-0"}  # doc-1 held back (crop not uploaded)


def test_publish_db_only_uploaded_empty_frontier_returns_none(tmp_path: Path) -> None:
    out = tmp_path / "out"
    db = out / "results" / "results.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY, source_path TEXT)")
    con.execute("CREATE TABLE vote_fields (id INTEGER PRIMARY KEY, document_id TEXT, page_number INTEGER, "
                "row_number INTEGER, row_type TEXT, section TEXT, candidate_number INTEGER, "
                "candidate_name TEXT, raw_crop_path TEXT, vlm_classification TEXT)")
    con.execute("INSERT INTO documents VALUES ('d', 'd.pdf')")
    con.execute("INSERT INTO vote_fields (document_id, page_number, row_number, row_type, raw_crop_path) "
                "VALUES ('d',1,1,'candidate','data/x/crops/d.png')")
    con.commit(); con.close()
    (out / "review").mkdir(parents=True, exist_ok=True)
    (out / "review" / "uploaded_crops.txt").write_text("")  # nothing uploaded

    sentinel = object()
    class _NoS3:
        def upload_file(self, *a, **k): raise AssertionError("should not upload")
        def put_object(self, *a, **k): raise AssertionError("should not flip pointer")
    assert dbsync.publish_db(out, bucket="b", client=_NoS3(), only_uploaded=True, verbose=False) is None


def test_publish_db_refuses_to_shrink_live_db(tmp_path: Path) -> None:
    """Guard: a much smaller new DB (wrong --output-dir) must NOT flip the live pointer."""
    out = tmp_path / "out"
    _make_db(out / "results" / "results.sqlite", 2)  # tiny "stub"

    class _FakeS3:
        def __init__(self) -> None:
            # Pretend a big DB is already live (raw_size far above the stub's).
            self.objects = {dbsync.POINTER_KEY: json.dumps(
                {"key": "db/results-big.sqlite.gz", "sha256": "a" * 64,
                 "size": 60_000_000, "raw_size": 800_000_000}).encode()}

        def get_object(self, Bucket, Key):  # noqa: N803
            return {"Body": type("B", (), {"read": lambda s: self.objects[Key]})()}

        def upload_file(self, *a, **k):
            raise AssertionError("must not upload a shrinking DB")

        def put_object(self, *a, **k):  # noqa: N803
            raise AssertionError("must not flip the pointer to a shrinking DB")

    s3 = _FakeS3()
    info = dbsync.publish_db(out, bucket="b", client=s3, verbose=False)
    assert info is not None and info.get("guarded") is True

    # With allow_shrink=True the override publishes normally.
    class _OkS3(_FakeS3):
        def upload_file(self, local, bucket, key, ExtraArgs=None):  # noqa: N803
            self.objects[key] = Path(local).read_bytes()

        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            self.objects[Key] = Body

    info2 = dbsync.publish_db(out, bucket="b", client=_OkS3(), allow_shrink=True, verbose=False)
    assert info2 is not None and not info2.get("guarded")


def test_merge_results_db_union_without_overwriting_local(tmp_path: Path) -> None:
    """Remote actas are merged in; local actas are kept; vote_fields follow new docs only."""
    local = tmp_path / "local" / "results.sqlite"
    remote = tmp_path / "remote" / "results.sqlite"
    _make_db(local, 2)  # d0, d1
    _make_db(remote, 3)  # d2, d3, d4 (cumulative ids after local's 2 rows)

    stats = dbsync.merge_results_db(local, remote, verbose=False)
    assert stats["docs_added"] == 1  # remote d2 only (d0,d1 already local)
    assert stats["fields_added"] == 1
    con = sqlite3.connect(local)
    docs = {r[0] for r in con.execute("SELECT document_id FROM documents")}
    assert docs == {"d0", "d1", "d2"}
    assert con.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0] == 3
    con.close()


def test_publish_db_allows_smaller_bytes_when_acta_count_holds(tmp_path: Path) -> None:
    """Regression: a slimmer-but-complete snapshot (fewer bytes, same/more actas) must
    publish. The guard keys on acta count, not raw bytes, so a schema slim-down never trips it."""
    out = tmp_path / "out"
    _make_db(out / "results" / "results.sqlite", 60)  # 60 actas, tiny in bytes

    class _OkS3:
        def __init__(self) -> None:
            # New-style live pointer: 100 actas, but a huge raw_size from an un-slimmed build.
            self.objects = {dbsync.POINTER_KEY: json.dumps(
                {"key": "db/results-big.sqlite.gz", "sha256": "a" * 64,
                 "size": 60_000_000, "raw_size": 800_000_000, "n_docs": 100}).encode()}

        def get_object(self, Bucket, Key):  # noqa: N803
            return {"Body": type("B", (), {"read": lambda s: self.objects[Key]})()}

        def upload_file(self, local, bucket, key, ExtraArgs=None):  # noqa: N803
            self.objects[key] = Path(local).read_bytes()

        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            self.objects[Key] = Body

    s3 = _OkS3()
    info = dbsync.publish_db(out, bucket="b", client=s3, verbose=False)
    # 60 actas >= 0.5*100, so it publishes despite being a fraction of the live bytes.
    assert info is not None and not info.get("guarded")
    assert info["n_docs"] == 60
    pointer = json.loads(s3.objects[dbsync.POINTER_KEY])
    assert pointer["n_docs"] == 60  # pointer now carries the count for future count-based guards


def test_publish_db_refuses_when_acta_count_drops(tmp_path: Path) -> None:
    """Count-based guard: a real drop in actas (wrong --output-dir / stub) is still refused."""
    out = tmp_path / "out"
    _make_db(out / "results" / "results.sqlite", 40)  # 40 actas

    class _FakeS3:
        def __init__(self) -> None:
            self.objects = {dbsync.POINTER_KEY: json.dumps(
                {"key": "db/results-big.sqlite.gz", "sha256": "a" * 64,
                 "size": 60_000_000, "raw_size": 800_000_000, "n_docs": 100}).encode()}

        def get_object(self, Bucket, Key):  # noqa: N803
            return {"Body": type("B", (), {"read": lambda s: self.objects[Key]})()}

        def upload_file(self, *a, **k):
            raise AssertionError("must not upload a DB that drops actas")

        def put_object(self, *a, **k):  # noqa: N803
            raise AssertionError("must not flip the pointer to a DB that drops actas")

    info = dbsync.publish_db(out, bucket="b", client=_FakeS3(), verbose=False)
    assert info is not None and info.get("guarded") is True  # 40 < 0.5*100


def test_publish_db_refuses_when_locked(tmp_path: Path) -> None:
    """A locked db/lock.json freezes the served DB: publish refuses unless allow_locked."""
    out = tmp_path / "out"
    _make_db(out / "results" / "results.sqlite", 3)

    class _FakeS3:
        def __init__(self) -> None:
            self.objects = {dbsync.LOCK_KEY: json.dumps({"locked": True, "reason": "done"}).encode()}

        def get_object(self, Bucket, Key):  # noqa: N803
            if Key not in self.objects:
                raise KeyError(Key)
            return {"Body": type("B", (), {"read": lambda s: self.objects[Key]})()}

        def upload_file(self, local, bucket, key, ExtraArgs=None):  # noqa: N803
            self.objects[key] = Path(local).read_bytes()

        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            self.objects[Key] = Body

    s3 = _FakeS3()
    info = dbsync.publish_db(out, bucket="b", client=s3, verbose=False)
    assert info is not None and info.get("locked") is True
    assert dbsync.POINTER_KEY not in s3.objects  # pointer never flipped

    # allow_locked overrides the lock and publishes normally.
    info2 = dbsync.publish_db(out, bucket="b", client=s3, allow_locked=True, allow_shrink=True, verbose=False)
    assert info2 is not None and not info2.get("locked")
    assert dbsync.POINTER_KEY in s3.objects


def test_set_and_read_db_lock_round_trip(tmp_path: Path) -> None:
    class _FakeS3:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            self.objects[Key] = Body

        def get_object(self, Bucket, Key):  # noqa: N803
            if Key not in self.objects:
                raise KeyError(Key)
            return {"Body": type("B", (), {"read": lambda s: self.objects[Key]})()}

    s3 = _FakeS3()
    assert dbsync.read_db_lock(client=s3, bucket="b") == {"locked": False}  # absent -> unlocked
    dbsync.set_db_lock(True, reason="100%", n_docs=122007, client=s3, bucket="b")
    got = dbsync.read_db_lock(client=s3, bucket="b")
    assert got["locked"] is True and got["n_docs"] == 122007


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
    assert info["key"] == f"db/results-{info['sha256'][:16]}.sqlite.gz"
    assert info["key"] in s3.objects
    pointer = json.loads(s3.objects[dbsync.POINTER_KEY])
    assert pointer["sha256"] == info["sha256"] and pointer["key"] == info["key"]
    assert pointer["n_docs"] == info["n_docs"] == 2  # count recorded for the count-based guard
    # Both totals shipped, and every doc here is browsable (has a candidate crop) so they match.
    assert pointer["n_browsable"] == info["n_browsable"] == 2


def test_publish_db_records_browsable_count_and_gap(tmp_path: Path) -> None:
    """The pointer carries n_browsable (actas with >=1 candidate crop) next to n_docs so the admin
    board reconciles 'servidas' vs 'publicadas' from one source. Docs with no candidate crop
    (n_candidates=0) widen the n_docs-vs-n_browsable gap — they must not count as browsable."""
    out = tmp_path / "out"
    db = out / "results" / "results.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY, source_path TEXT)")
    con.execute(
        "CREATE TABLE vote_fields (id INTEGER PRIMARY KEY, document_id TEXT, page_number INTEGER, "
        "row_number INTEGER, row_type TEXT, section TEXT, candidate_number INTEGER, "
        "candidate_name TEXT, raw_crop_path TEXT, vlm_classification TEXT)")
    # 3 browsable docs: a candidate row WITH a crop path.
    for d in ("d0", "d1", "d2"):
        con.execute("INSERT INTO documents VALUES (?, ?)", (d, f"{d}.pdf"))
        con.execute("INSERT INTO vote_fields (document_id, page_number, row_number, row_type, raw_crop_path) "
                    "VALUES (?,1,1,'candidate',?)", (d, f"crops/{d}.png"))
    # 2 metadata-only docs (n_candidates=0): one with no vote_fields, one with only a summary row.
    con.execute("INSERT INTO documents VALUES ('g0', 'g0.pdf')")
    con.execute("INSERT INTO documents VALUES ('g1', 'g1.pdf')")
    con.execute("INSERT INTO vote_fields (document_id, page_number, row_number, row_type, raw_crop_path) "
                "VALUES ('g1',2,14,'summary',NULL)")
    con.commit(); con.close()

    class _FakeS3:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def upload_file(self, local, bucket, key, ExtraArgs=None):  # noqa: N803
            self.objects[key] = Path(local).read_bytes()

        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            self.objects[Key] = Body

    s3 = _FakeS3()
    info = dbsync.publish_db(out, bucket="b", client=s3, verbose=False)
    assert info["n_docs"] == 5 and info["n_browsable"] == 3  # 2 metadata-only docs in the gap
    pointer = json.loads(s3.objects[dbsync.POINTER_KEY])
    assert pointer["n_docs"] == 5 and pointer["n_browsable"] == 3
    assert pointer["n_browsable"] <= pointer["n_docs"]  # the invariant the admin board relies on


def _make_geo_db(path: Path, served_keys: list[tuple[str, str, str, str, str]]) -> None:
    """A results DB whose documents carry geo codes (dep,muni,zona,puesto,mesa) so the
    reconciliation can diff served keys against the universe's informed keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY, source_path TEXT, "
                "department_code TEXT, municipality_code TEXT, zone TEXT, puesto TEXT, mesa TEXT)")
    con.execute("CREATE TABLE vote_fields (id INTEGER PRIMARY KEY, document_id TEXT, page_number INTEGER, "
                "row_number INTEGER, row_type TEXT, section TEXT, candidate_number INTEGER, "
                "candidate_name TEXT, raw_crop_path TEXT, vlm_classification TEXT)")
    for i, (dep, muni, zona, puesto, mesa) in enumerate(served_keys):
        d = f"doc-{i}"
        con.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?)", (d, f"{d}.pdf", dep, muni, zona, puesto, mesa))
        con.execute("INSERT INTO vote_fields (document_id, page_number, row_number, row_type, raw_crop_path) "
                    "VALUES (?,1,1,'candidate',?)", (d, f"crops/{d}.png"))
    con.commit(); con.close()


def _write_universe_snapshot(path: Path, total_global: int, keys: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "total_global": total_global, "mesas_informadas": len(keys),
        "fetched_at": "2026-06-05T00:00:00+00:00", "keys": sorted(keys)}))


def test_publish_db_stamps_reconciliation_chain(tmp_path: Path, monkeypatch) -> None:
    """The publisher stamps the count-model chain into the pointer: total_global / informadas
    from the universe snapshot, sqlite_served from the snapshot, and the derived ingest backlog
    (informadas − served) with a sample of which informed mesas aren't served yet."""
    from e14 import universe

    out = tmp_path / "out"
    # Serve 3 mesas; the universe knows 4 informed mesas (1 is behind) out of 6 total_global.
    served = [("01", "001", "001", "01", "001"), ("01", "001", "001", "01", "002"),
              ("01", "001", "001", "01", "003")]
    _make_geo_db(out / "results" / "results.sqlite", served)
    informed_keys = [f"01_001_001_01_{m}" for m in ("001", "002", "003", "004")]  # 004 is the backlog
    snap_path = tmp_path / "universe_snapshot.json"
    _write_universe_snapshot(snap_path, total_global=6, keys=informed_keys)
    monkeypatch.setattr(universe, "SNAPSHOT_PATH", snap_path)

    class _FakeS3:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def upload_file(self, local, bucket, key, ExtraArgs=None):  # noqa: N803
            self.objects[key] = Path(local).read_bytes()

        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            self.objects[Key] = Body

    s3 = _FakeS3()
    info = dbsync.publish_db(out, bucket="b", client=s3, verbose=False)
    recon = info["reconciliation"]
    assert recon["total_global"] == 6
    assert recon["mesas_informadas"] == 4
    assert recon["sqlite_served"] == 3
    assert recon["backlog_ingesta"] == 1   # 4 informed − 3 served
    assert recon["backlog_reporte"] == 2   # 6 total − 4 informed
    assert recon["missing_count"] == 1
    assert recon["missing_keys_sample"] == ["01_001_001_01_004"]
    # The pointer on the bucket carries the same block.
    pointer = json.loads(s3.objects[dbsync.POINTER_KEY])
    assert pointer["reconciliation"]["backlog_ingesta"] == 1


def test_publish_db_force_pointer_restamps_unchanged_db(tmp_path: Path, monkeypatch) -> None:
    """A frozen/locked round's DB is unchanged (same sha), so a normal publish is a no-op. With
    force_pointer the pointer is re-stamped with a fresh reconciliation block — no re-upload."""
    from e14 import universe

    out = tmp_path / "out"
    served = [("01", "001", "001", "01", "001"), ("01", "001", "001", "01", "002")]
    _make_geo_db(out / "results" / "results.sqlite", served)
    snap_path = tmp_path / "universe_snapshot.json"
    _write_universe_snapshot(snap_path, total_global=5, keys=[f"01_001_001_01_{m}" for m in ("001", "002", "003")])
    monkeypatch.setattr(universe, "SNAPSHOT_PATH", snap_path)

    class _FakeS3:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.uploads = 0

        def get_object(self, Bucket, Key):  # noqa: N803
            if Key not in self.objects:
                raise KeyError(Key)
            return {"Body": type("B", (), {"read": lambda s: self.objects[Key]})()}

        def upload_file(self, local, bucket, key, ExtraArgs=None):  # noqa: N803
            self.uploads += 1
            self.objects[key] = Path(local).read_bytes()

        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            self.objects[Key] = Body

    s3 = _FakeS3()
    first = dbsync.publish_db(out, bucket="b", client=s3, verbose=False)
    uploads_after_first = s3.uploads
    assert first["reconciliation"]["sqlite_served"] == 2

    # Re-publishing the same DB without force_pointer is a skip (no re-upload, no re-stamp).
    skipped = dbsync.publish_db(out, bucket="b", client=s3, verbose=False)
    assert skipped.get("skipped") is True and s3.uploads == uploads_after_first

    # With force_pointer it re-stamps the pointer (fresh ts) but does NOT re-upload the snapshot.
    restamped = dbsync.publish_db(out, bucket="b", client=s3, force_pointer=True, verbose=False)
    assert restamped.get("restamped") is True
    assert s3.uploads == uploads_after_first  # snapshot object untouched
    pointer = json.loads(s3.objects[dbsync.POINTER_KEY])
    assert pointer["reconciliation"]["sqlite_served"] == 2
    assert pointer["sha256"] == first["sha256"]  # same content, just a refreshed pointer


def test_build_serving_db_drops_fat_columns_and_tables(tmp_path: Path) -> None:
    """The serving snapshot keeps only the registry the live site reads: fat columns
    (debug paths, cv_score, vlm_raw_json) and whole working tables (cv_features) are gone,
    while id values and row counts are preserved so the feed's random-PK sampling works."""
    src = tmp_path / "src" / "results.sqlite"
    _make_db(src, 7)
    dest = tmp_path / "serve.sqlite"
    digest = dbsync.build_serving_db(src, dest)
    assert len(digest) == 64 and dest.exists()

    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "vote_fields" in tables and "documents" in tables
        assert "cv_features" not in tables  # working table dropped

        cols = {r[1] for r in con.execute("PRAGMA table_info(vote_fields)")}
        assert {"id", "document_id", "page_number", "row_number", "row_type", "section",
                "candidate_number", "candidate_name", "raw_crop_path", "vlm_classification"} <= cols
        assert {"debug_crop_path", "cv_score", "vlm_raw_json"} & cols == set()  # fat columns dropped

        # Registry preserved: row count + dense ids intact.
        assert con.execute("SELECT COUNT(*) FROM vote_fields").fetchone()[0] == 7
        ids = [r[0] for r in con.execute("SELECT id FROM vote_fields ORDER BY id")]
        assert ids == list(range(1, 8))
        assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 7

        # Indexes the live queries rely on are present.
        idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_vf_doc_type", "idx_vf_crop", "idx_doc_geo"} <= idx
    finally:
        con.close()
