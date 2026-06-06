"""Unified `e14 sync` orchestration: the invariant-chain verifier and the local status path."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from e14detector import sync


def _served_db(output_dir: Path, n: int) -> None:
    db = output_dir / "results" / "results.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY, source_path TEXT, "
                "department_code TEXT, municipality_code TEXT, zone TEXT, puesto TEXT, mesa TEXT)")
    for i in range(n):
        con.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?)",
                    (f"d{i}", f"d{i}.pdf", "01", "001", "001", "01", str(i).zfill(3)))
    con.commit(); con.close()


def _served_db_with_crops(output_dir: Path, crop_paths: list[str]) -> None:
    db = output_dir / "results" / "results.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY, source_path TEXT)")
    con.execute("CREATE TABLE vote_fields (id INTEGER PRIMARY KEY, document_id TEXT, page_number INTEGER, "
                "row_number INTEGER, row_type TEXT, raw_crop_path TEXT)")
    for i, p in enumerate(crop_paths):
        con.execute("INSERT INTO vote_fields (document_id, page_number, row_number, row_type, raw_crop_path) "
                    "VALUES (?,1,?,?,?)", (f"d{i}", i, "candidate", p))
    con.commit(); con.close()


def test_audit_served_crops_clean_when_all_present(tmp_path: Path, monkeypatch) -> None:
    from e14detector import cropaudit, publish

    out = tmp_path / "out"
    _served_db_with_crops(out, ["data/x/crops/a.png", "data/x/crops/b.png"])
    monkeypatch.setattr(publish, "list_bucket_crop_keys",
                        lambda **kw: {"crops/a.png", "crops/b.png"})
    assert cropaudit.audit_served_crops(out, bucket="b") == []


def test_audit_served_crops_flags_orphans(tmp_path: Path, monkeypatch) -> None:
    from e14detector import cropaudit, publish

    out = tmp_path / "out"
    _served_db_with_crops(out, ["data/x/crops/a.png", "data/x/crops/b.png", "data/x/crops/c.png"])
    monkeypatch.setattr(publish, "list_bucket_crop_keys", lambda **kw: {"crops/a.png"})
    problems = cropaudit.audit_served_crops(out, bucket="b")
    assert len(problems) == 1 and "2 recorte" in problems[0]  # b.png + c.png missing


def test_content_summary_parses_latest_report(tmp_path: Path) -> None:
    from e14detector import contentcheck

    reports = tmp_path / "reports"
    reports.mkdir()
    # Older report (should be ignored in favour of the newest by filename sort).
    (reports / "content_verify_20260601T000000Z.csv").write_text(
        "key,verdict\nk1,match\n")
    (reports / "content_verify_20260605T000000Z.csv").write_text(
        "key,verdict\nk1,match\nk2,content_changed\nk3,content_changed\nk4,no_baseline\n")
    s = contentcheck.latest_content_summary(reports)
    assert s["report"] == "content_verify_20260605T000000Z.csv"
    assert s["checked"] == 4 and s["content_changed"] == 2
    assert s["changed_pct"] == 50.0
    note = contentcheck.content_note(reports)
    assert "no edición probada" in note


def test_content_summary_none_when_no_reports(tmp_path: Path) -> None:
    from e14detector import contentcheck
    assert contentcheck.latest_content_summary(tmp_path / "nope") is None
    assert contentcheck.content_note(tmp_path / "nope") is None


def test_stamp_pointer_patches_live_pointer_without_rebuild(tmp_path: Path, monkeypatch) -> None:
    """stamp-pointer adds the reconciliation block to the live pointer, preserving n_docs/sha and
    never rebuilding the snapshot (safe for a locked round whose local DB may be stale)."""
    import json as _json

    from e14 import universe
    from e14detector import dbsync, sync

    monkeypatch.chdir(tmp_path)
    # A "live" snapshot DB the download step will hand back (2 served mesas).
    live_db = tmp_path / "live.sqlite"
    con = sqlite3.connect(live_db)
    con.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY, source_path TEXT, "
                "department_code TEXT, municipality_code TEXT, zone TEXT, puesto TEXT, mesa TEXT)")
    for m in ("001", "002"):
        con.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?)",
                    (f"d{m}", "x", "01", "001", "001", "01", m))
    con.commit(); con.close()
    # Universe snapshot: 4 informed, total_global 6, escrutadas 5 -> backlog ingesta = 4-2 = 2.
    snap = tmp_path / "universe_snapshot.json"
    snap.write_text(_json.dumps({"total_global": 6, "mesas_escrutadas": 5, "mesas_informadas": 4,
                    "fetched_at": "2026-06-05T00:00:00+00:00",
                    "keys": [f"01_001_001_01_{m}" for m in ("001", "002", "003", "004")]}))
    monkeypatch.setattr(universe, "SNAPSHOT_PATH", snap)

    stored: dict[str, bytes] = {}

    class _Client:
        def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
            stored[Key] = Body

    live_pointer = {"key": "db/results-abc.sqlite.gz", "sha256": "a" * 64, "size": 100,
                    "raw_size": 200, "n_docs": 2}
    monkeypatch.setattr(dbsync, "_s3_client", lambda: _Client())
    monkeypatch.setattr(dbsync, "fetch_published_pointer", lambda **kw: dict(live_pointer))
    monkeypatch.setattr(dbsync, "_download_snapshot_file",
                        lambda pointer, dest, **kw: __import__("shutil").copy(live_db, dest))

    rc = sync.do_stamp_pointer(tmp_path / "out", bucket="e14-crops", cdn_base="http://cdn")
    assert rc == 0
    patched = _json.loads(stored[dbsync.POINTER_KEY])
    assert patched["n_docs"] == 2 and patched["sha256"] == "a" * 64  # preserved, not rebuilt
    recon = patched["reconciliation"]
    assert recon["total_global"] == 6 and recon["mesas_escrutadas"] == 5
    assert recon["mesas_informadas"] == 4 and recon["sqlite_served"] == 2
    assert recon["backlog_ingesta"] == 2 and recon["backlog_reporte"] == 2
    assert recon["missing_count"] == 2  # 003 + 004 informed but not served


def test_verify_chain_passes_on_consistent_counts() -> None:
    recon = {"total_global": 122020, "mesas_informadas": 122020, "downloaded": 122010,
             "crops_uploaded": 122007, "sqlite_served": 122007, "backlog_ingesta": 13}
    rep = sync.verify_chain(recon, served_count=122007, published=122007)
    assert rep.ok
    assert any("backlog de ingesta = 13" in n for n in rep.notes)


def test_verify_chain_flags_inversion() -> None:
    rep = sync.verify_chain({"mesas_informadas": 100, "sqlite_served": 120},
                            served_count=120, published=120)
    assert not rep.ok and any("inversión" in p for p in rep.problems)


def test_verify_chain_flags_served_not_equal_published() -> None:
    rep = sync.verify_chain({"sqlite_served": 100}, served_count=100, published=99)
    assert not rep.ok and any("publicada" in p for p in rep.problems)


def test_verify_chain_skips_unknown_counts() -> None:
    # Only the external anchors + served are present; downloaded/crops_uploaded unknown -> skipped.
    rep = sync.verify_chain({"total_global": 100, "mesas_informadas": 100, "sqlite_served": 90},
                            served_count=90, published=90)
    assert rep.ok


def test_do_status_local_fallback_reads_served_db(tmp_path: Path, monkeypatch, capsys) -> None:
    """With no pointer, status computes the chain locally from the served DB + universe snapshot."""
    from e14 import universe

    monkeypatch.chdir(tmp_path)  # isolate from the repo's real data/manifest.db
    out = tmp_path / "out"
    _served_db(out, 3)
    snap = tmp_path / "universe_snapshot.json"
    snap.write_text('{"total_global": 6, "mesas_informadas": 4, "fetched_at": "2026-06-05T00:00:00+00:00",'
                    ' "keys": ["01_001_001_01_000","01_001_001_01_001","01_001_001_01_002","01_001_001_01_009"]}')
    monkeypatch.setattr(universe, "SNAPSHOT_PATH", snap)

    rc = sync.do_status(out, cdn_base=None)
    text = capsys.readouterr().out
    assert rc == 0
    assert "Total nacional" in text and "cobertura" in text
    assert "cálculo local" in text  # used the local fallback, not a pointer


def test_do_verify_local_detects_backlog_but_passes(tmp_path: Path, monkeypatch, capsys) -> None:
    from e14 import universe

    monkeypatch.chdir(tmp_path)  # isolate from the repo's real data/manifest.db
    out = tmp_path / "out"
    _served_db(out, 3)
    snap = tmp_path / "universe_snapshot.json"
    snap.write_text('{"total_global": 6, "mesas_informadas": 4, "fetched_at": "2026-06-05T00:00:00+00:00",'
                    ' "keys": ["01_001_001_01_000","01_001_001_01_001","01_001_001_01_002","01_001_001_01_009"]}')
    monkeypatch.setattr(universe, "SNAPSHOT_PATH", snap)

    # No cdn_base -> no published comparison; the chain itself is consistent (4>=3), so verify passes.
    rc = sync.do_verify(out, bucket=None, cdn_base=None)
    assert rc == 0
    assert "backlog de ingesta = 1" in capsys.readouterr().out
