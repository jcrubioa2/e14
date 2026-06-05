"""Content-integrity axis (P1.C).

Coverage answers "is the acta present?"; content integrity answers "does the served crop still
match the *current* official PDF?" The registraduría silently re-issues some acta PDFs under a
stable URL hash (observed ~5.3%), so a served crop can go stale even at 100% coverage. The heavy
re-fetch sweep lives in ``scripts/verify_acta_content.py`` and writes a timestamped report to
``data/reports/content_verify_*.csv``; this module just *summarizes the latest report* so
``e14 sync verify --check-content`` and the admin chain can surface the content axis cheaply,
without re-fetching anything inline.

Important: a content change is **re-emission, not proven tally edit** — it is informational, NOT
a coverage failure, so it is surfaced as a note, never a verify error.
"""
from __future__ import annotations

import csv
from pathlib import Path

CONTENT_CHANGED = "content_changed"


def latest_content_summary(reports_dir: Path = Path("data") / "reports") -> dict | None:
    """Summarize the newest ``content_verify_*.csv`` report, or None if there is none.

    Returns ``{report, ts, checked, match, content_changed, no_baseline, error, changed_pct}``.
    The timestamp is parsed from the report filename (``content_verify_<TS>.csv``).
    """
    reports_dir = Path(reports_dir)
    if not reports_dir.exists():
        return None
    reports = sorted(reports_dir.glob("content_verify_*.csv"))
    if not reports:
        return None
    report = reports[-1]
    counts: dict[str, int] = {}
    total = 0
    try:
        with report.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                v = (row.get("verdict") or "").strip()
                if not v:
                    continue
                counts[v] = counts.get(v, 0) + 1
                total += 1
    except Exception:  # noqa: BLE001 — a malformed report just yields "no summary"
        return None
    if total == 0:
        return None
    changed = counts.get(CONTENT_CHANGED, 0)
    return {
        "report": report.name,
        "ts": report.stem.replace("content_verify_", ""),
        "checked": total,
        "match": counts.get("match", 0),
        "content_changed": changed,
        "no_baseline": counts.get("no_baseline", 0),
        "error": counts.get("error", 0),
        "changed_pct": round(changed * 100 / total, 2),
    }


def content_note(reports_dir: Path = Path("data") / "reports") -> str | None:
    """A one-line human summary for the operator terminal / admin board, or None if no report."""
    s = latest_content_summary(reports_dir)
    if not s:
        return None
    return (f"integridad de contenido: {s['content_changed']} de {s['checked']} re-emitidos "
            f"({s['changed_pct']}%) — re-emisión, no edición probada · informe {s['report']}")
