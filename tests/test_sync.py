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
