"""Universe snapshot — the external source of truth for the count model (total_global +
mesas_informadas), and the shrink-guard that refuses a truncated fetch."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from e14 import universe
from e14.universe import (
    ActaRecord, UniverseShrinkError, load_universe_snapshot, write_universe_snapshot,
)


def _rec(dep: str, muni: str, zona: str, puesto: str, mesa: str) -> ActaRecord:
    return ActaRecord(
        dep=dep, muni=muni, zona=zona, puesto=puesto, mesa=mesa, corp="001",
        expected_name=f"{dep}{muni}{mesa}.pdf", id_stand="1", id_transmission_code="1", status=11)


def test_snapshot_round_trip_records_both_counts(tmp_path: Path) -> None:
    path = tmp_path / "universe_snapshot.json"
    recs = [_rec("01", "001", "001", "01", str(i).zfill(3)) for i in range(5)]
    snap = write_universe_snapshot(recs, total_global=8, path=path)
    assert snap["total_global"] == 8
    assert snap["mesas_informadas"] == 5  # len(records)
    assert snap["keys"] == sorted(r.key for r in recs)
    assert "fetched_at" in snap

    loaded = load_universe_snapshot(path)
    assert loaded == snap
    # On-disk JSON is the same record.
    assert json.loads(path.read_text())["total_global"] == 8


def test_load_missing_snapshot_returns_none(tmp_path: Path) -> None:
    assert load_universe_snapshot(tmp_path / "nope.json") is None


def test_shrink_guard_refuses_truncated_fetch(tmp_path: Path) -> None:
    path = tmp_path / "universe_snapshot.json"
    big = [_rec("01", "001", "001", "01", str(i).zfill(3)) for i in range(100)]
    write_universe_snapshot(big, total_global=120, path=path)

    # A fetch that collapses to a fraction of the accepted universe must be refused.
    tiny = [_rec("01", "001", "001", "01", str(i).zfill(3)) for i in range(10)]
    with pytest.raises(UniverseShrinkError):
        write_universe_snapshot(tiny, total_global=12, path=path)
    # The accepted snapshot on disk is untouched.
    assert load_universe_snapshot(path)["total_global"] == 120

    # allow_shrink overrides (a real, deliberate universe correction).
    snap = write_universe_snapshot(tiny, total_global=12, path=path, allow_shrink=True)
    assert snap["total_global"] == 12


def test_shrink_guard_allows_growth_and_small_dips(tmp_path: Path) -> None:
    path = tmp_path / "universe_snapshot.json"
    base = [_rec("01", "001", "001", "01", str(i).zfill(3)) for i in range(100)]
    write_universe_snapshot(base, total_global=100, path=path)
    # Growth is always fine.
    grown = [_rec("01", "001", "001", "01", str(i).zfill(3)) for i in range(110)]
    assert write_universe_snapshot(grown, total_global=110, path=path)["mesas_informadas"] == 110
    # A small dip (above the 50% floor) is accepted — the guard only catches collapses.
    dip = [_rec("01", "001", "001", "01", str(i).zfill(3)) for i in range(90)]
    assert write_universe_snapshot(dip, total_global=109, path=path)["mesas_informadas"] == 90


def test_fetch_universe_counts_splits_total_from_informed(monkeypatch) -> None:
    """total_global counts every node; informed records keep only those with expectedName."""
    payload = {"data": {"block": {"nodes": [
        {"idDepartmentCode": "1", "municipalityCode": "1", "idZoneCode": "1", "standCode": "1",
         "numberStand": "1", "idCorporationCode": "001", "expectedName": "a.pdf",
         "idTransmissionCodeStatus": 11},
        {"idDepartmentCode": "1", "municipalityCode": "1", "idZoneCode": "1", "standCode": "1",
         "numberStand": "2", "idCorporationCode": "001", "expectedName": "b.pdf",
         "idTransmissionCodeStatus": 11},
        # Not yet informed: no expectedName -> counts toward total_global only.
        {"idDepartmentCode": "1", "municipalityCode": "1", "idZoneCode": "1", "standCode": "1",
         "numberStand": "3", "idCorporationCode": "001", "expectedName": "",
         "idTransmissionCodeStatus": 3},
    ]}}}

    class _Session:
        def get_json(self, url):  # noqa: ARG002
            return payload

    recs, total_global = universe.fetch_universe_counts(session=_Session())
    assert total_global == 3
    assert len(recs) == 2  # only the two with expectedName are "informed"
