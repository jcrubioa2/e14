import csv
import json
import sqlite3
from pathlib import Path

import pytest

from e14detector import fleet


def _write_universe(path: Path, counts: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["dep", "document_id"])
        w.writeheader()
        for dep, n in counts.items():
            for i in range(n):
                w.writerow({"dep": dep, "document_id": f"{dep}-{i}"})


def _write_db(path: Path, dept_counts: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE documents (document_id TEXT PRIMARY KEY, department_code TEXT, department_name TEXT)"
    )
    for dep, n in dept_counts.items():
        for i in range(n):
            con.execute(
                "INSERT INTO documents (document_id, department_code) VALUES (?, ?)",
                (f"{dep}-{i}", dep),
            )
    con.commit()
    con.close()


def test_schedule_assigns_largest_pending_first(tmp_path: Path) -> None:
    uni = tmp_path / "mesa_universe.csv"
    _write_universe(uni, {"10": 5, "20": 50, "30": 10})
    q = fleet.new_queue(uni, workers=["a", "b"])
    assigned = fleet.schedule_assignments(q, ["a", "b"])
    assert assigned[0][1] == "20"
    assert assigned[1][1] == "30"
    assert fleet.current_assignment(q, "a") == "20"
    assert fleet.current_assignment(q, "b") == "30"


def test_finish_department_marks_done_when_db_complete(tmp_path: Path) -> None:
    uni = tmp_path / "mesa_universe.csv"
    db = tmp_path / "results.sqlite"
    _write_universe(uni, {"05": 3})
    _write_db(db, {"05": 3})
    q = fleet.new_queue(uni, results_db=db, workers=["w1"])
    fleet.assign_worker(q, "w1", "05")
    status = fleet.finish_department(q, "05", results_db=db, worker_id="w1")
    assert status == "done"
    assert q["departments"]["05"]["status"] == "done"
    assert fleet.current_assignment(q, "w1") is None


def test_merge_queues_keeps_latest_claim(tmp_path: Path) -> None:
    uni = tmp_path / "mesa_universe.csv"
    _write_universe(uni, {"11": 10})
    local = fleet.new_queue(uni, workers=["w1"])
    remote = fleet.new_queue(uni, workers=["w2"])
    fleet.assign_worker(remote, "w2", "11")
    merged = fleet.merge_queues(local, remote)
    assert merged["departments"]["11"]["worker"] == "w2"
    assert merged["departments"]["11"]["status"] == "claimed"
