"""Multi-machine department queue: assign, claim, complete, schedule."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

FLEET_VERSION = 1
FLEET_DIR = "fleet"
QUEUE_NAME = "queue.json"
POINTER_KEY = f"{FLEET_DIR}/latest.json"


def _now() -> float:
    return time.time()


def _zfill_dep(dep: str) -> str:
    return str(dep).zfill(2)


def default_queue_path(output_dir: Path) -> Path:
    return Path(output_dir) / FLEET_DIR / QUEUE_NAME


def load_universe_counts(universe_csv: Path) -> dict[str, int]:
    """Expected actas per department from mesa_universe.csv (``dep`` column)."""
    counts: dict[str, int] = {}
    with universe_csv.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            dep = _zfill_dep(row["dep"])
            counts[dep] = counts.get(dep, 0) + 1
    return counts


def load_done_counts(results_db: Path) -> dict[str, int]:
    if not results_db.is_file():
        return {}
    con = sqlite3.connect(f"file:{results_db.resolve()}?mode=ro", uri=True, timeout=60.0)
    try:
        rows = con.execute(
            "SELECT department_code, COUNT(*) FROM documents "
            "WHERE department_code IS NOT NULL AND department_code != '' "
            "GROUP BY department_code"
        ).fetchall()
        return {_zfill_dep(r[0]): int(r[1]) for r in rows}
    finally:
        con.close()


def _dept_remaining(info: dict[str, Any]) -> int:
    return max(0, int(info.get("expected", 0)) - int(info.get("done", 0)))


def _dept_complete(info: dict[str, Any]) -> bool:
    return _dept_remaining(info) == 0 and int(info.get("expected", 0)) > 0


def new_queue(
    universe_csv: Path,
    *,
    results_db: Path | None = None,
    workers: list[str] | None = None,
) -> dict[str, Any]:
    expected = load_universe_counts(universe_csv)
    done = load_done_counts(results_db) if results_db else {}
    departments: dict[str, Any] = {}
    for dep in sorted(expected):
        dn = done.get(dep, 0)
        departments[dep] = {
            "expected": expected[dep],
            "done": dn,
            "status": "done" if dn >= expected[dep] else "pending",
            "worker": None,
            "claimed_at": None,
            "done_at": _now() if dn >= expected[dep] else None,
        }
    wmap: dict[str, Any] = {}
    for wid in workers or []:
        wmap[wid] = {"depto": None, "since": None, "role": "worker"}
    return {
        "version": FLEET_VERSION,
        "updated_at": _now(),
        "workers": wmap,
        "departments": departments,
    }


def load_queue(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_queue(path: Path, queue: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    queue["updated_at"] = _now()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def refresh_progress(queue: dict[str, Any], *, results_db: Path | None) -> None:
    """Update ``done`` counts and auto-complete departments from local DB."""
    if not results_db or not results_db.is_file():
        return
    done = load_done_counts(results_db)
    for dep, info in queue["departments"].items():
        dn = done.get(dep, 0)
        info["done"] = dn
        if _dept_complete(info):
            info["status"] = "done"
            info["worker"] = None
            info["claimed_at"] = None
            if not info.get("done_at"):
                info["done_at"] = _now()


def release_stale_claims(queue: dict[str, Any], *, stale_seconds: float) -> list[str]:
    """Reset ``claimed`` departments with no progress past ``stale_seconds``."""
    now = _now()
    released: list[str] = []
    for dep, info in queue["departments"].items():
        if info.get("status") != "claimed":
            continue
        claimed_at = float(info.get("claimed_at") or 0)
        if claimed_at <= 0 or (now - claimed_at) < stale_seconds:
            continue
        # Still actively cropping if done increased recently — use claimed_at only for v1
        info["status"] = "pending"
        info["worker"] = None
        info["claimed_at"] = None
        released.append(dep)
    for _wid, winfo in queue.get("workers", {}).items():
        depto = winfo.get("depto")
        if depto and queue["departments"].get(depto, {}).get("status") != "claimed":
            winfo["depto"] = None
            winfo["since"] = None
    return released


def pending_by_remaining(queue: dict[str, Any]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for dep, info in queue["departments"].items():
        if info.get("status") == "done":
            continue
        rem = _dept_remaining(info)
        if rem <= 0:
            info["status"] = "done"
            continue
        if info.get("status") == "pending":
            out.append((rem, dep))
    return sorted(out, reverse=True)


def merge_queues(local: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    """Union fleet state: preserve ``done``, prefer active ``claimed`` with latest ``claimed_at``."""
    merged = json.loads(json.dumps(local))
    for dep, rinfo in remote.get("departments", {}).items():
        linfo = merged["departments"].setdefault(dep, dict(rinfo))
        linfo["expected"] = max(int(linfo.get("expected", 0)), int(rinfo.get("expected", 0)))
        linfo["done"] = max(int(linfo.get("done", 0)), int(rinfo.get("done", 0)))
        if _dept_complete(linfo):
            linfo["status"] = "done"
            linfo["worker"] = None
            linfo["claimed_at"] = None
            continue
        rstatus = rinfo.get("status")
        lstatus = linfo.get("status")
        if rstatus == "done" or lstatus == "done":
            if _dept_complete(linfo):
                linfo["status"] = "done"
            continue
        if rstatus == "claimed" and lstatus != "claimed":
            linfo.update(
                {
                    "status": "claimed",
                    "worker": rinfo.get("worker"),
                    "claimed_at": rinfo.get("claimed_at"),
                }
            )
        elif rstatus == "claimed" and lstatus == "claimed":
            if float(rinfo.get("claimed_at") or 0) > float(linfo.get("claimed_at") or 0):
                linfo["worker"] = rinfo.get("worker")
                linfo["claimed_at"] = rinfo.get("claimed_at")
    for wid, rinfo in remote.get("workers", {}).items():
        merged["workers"].setdefault(wid, {}).update(
            {k: v for k, v in rinfo.items() if v is not None}
        )
    merged["updated_at"] = max(float(local.get("updated_at", 0)), float(remote.get("updated_at", 0)))
    return merged


def assign_worker(
    queue: dict[str, Any],
    worker_id: str,
    depto: str,
) -> None:
    dep = _zfill_dep(depto)
    info = queue["departments"][dep]
    info["status"] = "claimed"
    info["worker"] = worker_id
    info["claimed_at"] = _now()
    queue["workers"].setdefault(worker_id, {"role": "worker", "depto": None, "since": None})
    queue["workers"][worker_id]["depto"] = dep
    queue["workers"][worker_id]["since"] = _now()


def clear_worker_assignment(queue: dict[str, Any], worker_id: str) -> None:
    w = queue["workers"].get(worker_id)
    if not w:
        return
    depto = w.get("depto")
    if depto:
        info = queue["departments"].get(depto)
        if info and info.get("worker") == worker_id and info.get("status") == "claimed":
            if _dept_complete(info):
                info["status"] = "done"
                info["done_at"] = _now()
            else:
                info["status"] = "pending"
            info["worker"] = None
            info["claimed_at"] = None
    w["depto"] = None
    w["since"] = None


def mark_department_done(queue: dict[str, Any], depto: str, *, worker_id: str | None = None) -> None:
    dep = _zfill_dep(depto)
    info = queue["departments"][dep]
    info["status"] = "done"
    info["done_at"] = _now()
    info["worker"] = None
    info["claimed_at"] = None
    if worker_id and worker_id in queue.get("workers", {}):
        if queue["workers"][worker_id].get("depto") == dep:
            queue["workers"][worker_id]["depto"] = None
            queue["workers"][worker_id]["since"] = None


def finish_department(
    queue: dict[str, Any],
    depto: str,
    *,
    results_db: Path | None,
    worker_id: str | None = None,
) -> str:
    """Mark dept done if DB shows complete; otherwise release for reschedule. Returns status."""
    refresh_progress(queue, results_db=results_db)
    dep = _zfill_dep(depto)
    info = queue["departments"][dep]
    if _dept_complete(info):
        mark_department_done(queue, dep, worker_id=worker_id)
        return "done"
    clear_worker_assignment(queue, worker_id or info.get("worker") or "")
    info["status"] = "pending"
    return "pending"


def current_assignment(queue: dict[str, Any], worker_id: str) -> str | None:
    w = queue["workers"].get(worker_id)
    if not w:
        return None
    depto = w.get("depto")
    if not depto:
        return None
    info = queue["departments"].get(_zfill_dep(depto))
    if not info or info.get("status") == "done":
        return None
    return _zfill_dep(depto)


def worker_needs_work(queue: dict[str, Any], worker_id: str) -> bool:
    depto = current_assignment(queue, worker_id)
    if not depto:
        return True
    info = queue["departments"].get(depto)
    return bool(info and _dept_complete(info))


def schedule_assignments(
    queue: dict[str, Any],
    worker_ids: list[str],
    *,
    coordinator_id: str | None = None,
) -> list[tuple[str, str]]:
    """Assign next department to idle workers. Returns list of (worker, depto)."""
    release_stale_claims(queue, stale_seconds=float(os.environ.get("E14_FLEET_STALE_SEC", "7200")))
    assigned: list[tuple[str, str]] = []
    pending = pending_by_remaining(queue)
    pending_deps = [d for _, d in pending]

    for wid in worker_ids:
        queue["workers"].setdefault(wid, {"role": "worker", "depto": None, "since": None})
        if coordinator_id and wid == coordinator_id:
            queue["workers"][wid]["role"] = "coordinator"
        cur = current_assignment(queue, wid)
        if cur:
            info = queue["departments"][cur]
            if not _dept_complete(info):
                continue
            mark_department_done(queue, cur, worker_id=wid)
        if not pending_deps:
            continue
        depto = pending_deps.pop(0)
        assign_worker(queue, wid, depto)
        assigned.append((wid, depto))
    return assigned


def format_status(queue: dict[str, Any]) -> str:
    lines: list[str] = []
    deps = queue.get("departments", {})
    total_exp = sum(int(d.get("expected", 0)) for d in deps.values())
    total_done = sum(int(d.get("done", 0)) for d in deps.values())
    complete = sum(1 for d in deps.values() if d.get("status") == "done")
    claimed = [(d, i) for d, i in deps.items() if i.get("status") == "claimed"]
    pending_n = sum(1 for d in deps.values() if d.get("status") == "pending" and _dept_remaining(d) > 0)
    lines.append(
        f"fleet: {total_done:,}/{total_exp:,} actas · "
        f"{complete}/{len(deps)} depts done · {pending_n} pending · {len(claimed)} claimed"
    )
    for wid, w in sorted(queue.get("workers", {}).items()):
        depto = w.get("depto") or "-"
        role = w.get("role", "worker")
        lines.append(f"  {wid:16} {role:12} depto={depto}")
    for dep, info in sorted(claimed, key=lambda x: x[0]):
        rem = _dept_remaining(info)
        w = info.get("worker", "?")
        lines.append(f"  dept {dep} → {w} ({info.get('done', 0):,}/{info.get('expected', 0):,}, {rem:,} left)")
    return "\n".join(lines)
