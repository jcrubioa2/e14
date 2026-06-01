"""SQLite manifest: source of truth for fetch status + provenance.

One row per (mesa_key, variant). Provenance per init.md: source URL, HTTP
status, Content-Type, Content-Length, UTC fetch timestamp, SHA-256, byte size,
server filename, resolved geo codes, and failure reason.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS actas (
    key           TEXT NOT NULL,
    variant       TEXT NOT NULL,
    dep           TEXT, muni TEXT, zona TEXT, puesto TEXT, mesa TEXT, corp TEXT,
    expected_name TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed|skipped
    source_url    TEXT,
    http_status   INTEGER,
    content_type  TEXT,
    content_length INTEGER,
    sha256        TEXT,
    byte_size     INTEGER,
    server_filename TEXT,
    fetched_at_utc TEXT,
    reason        TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, variant)
);
CREATE INDEX IF NOT EXISTS idx_status ON actas(status);
"""


class Manifest:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def seed(self, records, variant: str) -> int:
        """Insert pending rows for records not yet present. Returns new count."""
        c = self._conn()
        n = 0
        c.execute("BEGIN")
        try:
            for r in records:
                cur = c.execute(
                    """INSERT OR IGNORE INTO actas
                       (key,variant,dep,muni,zona,puesto,mesa,corp,expected_name,status)
                       VALUES (?,?,?,?,?,?,?,?,?,'pending')""",
                    (r.key, variant, r.dep, r.muni, r.zona, r.puesto, r.mesa,
                     r.corp, r.expected_name),
                )
                n += cur.rowcount
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return n

    def status_of(self, key: str, variant: str) -> str | None:
        row = self._conn().execute(
            "SELECT status FROM actas WHERE key=? AND variant=?", (key, variant)
        ).fetchone()
        return row["status"] if row else None

    def pending_keys(self, variant: str, statuses=("pending", "failed"),
                     only_failed: bool = False) -> set[str]:
        wanted = ("failed",) if only_failed else statuses
        ph = ",".join("?" * len(wanted))
        rows = self._conn().execute(
            f"SELECT key FROM actas WHERE variant=? AND status IN ({ph})",
            (variant, *wanted),
        ).fetchall()
        return {r["key"] for r in rows}

    def mark_done(self, key, variant, **prov) -> None:
        self._update(key, variant, "done", **prov)

    def mark_failed(self, key, variant, reason, **prov) -> None:
        self._update(key, variant, "failed", reason=reason, **prov)

    def mark_skipped(self, key, variant, reason="") -> None:
        self._update(key, variant, "skipped", reason=reason)

    def _update(self, key, variant, status, **fields) -> None:
        cols = ["status=?"]
        vals = [status]
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(v)
        cols.append("attempts=attempts+1")
        vals += [key, variant]
        self._conn().execute(
            f"UPDATE actas SET {','.join(cols)} WHERE key=? AND variant=?", vals
        )

    def counts(self, variant: str | None = None) -> dict[str, int]:
        q = "SELECT status, COUNT(*) n FROM actas"
        args: tuple = ()
        if variant:
            q += " WHERE variant=?"
            args = (variant,)
        q += " GROUP BY status"
        return {r["status"]: r["n"] for r in self._conn().execute(q, args)}

    def total_bytes(self, variant: str | None = None) -> int:
        q = "SELECT COALESCE(SUM(byte_size),0) b FROM actas WHERE status='done'"
        args: tuple = ()
        if variant:
            q += " AND variant=?"
            args = (variant,)
        return int(self._conn().execute(q, args).fetchone()["b"])

    def export_failed(self, path: Path, variant: str | None = None) -> int:
        import csv
        q = ("SELECT key,variant,dep,muni,zona,puesto,mesa,expected_name,"
             "http_status,reason FROM actas WHERE status='failed'")
        args: tuple = ()
        if variant:
            q += " AND variant=?"
            args = (variant,)
        rows = self._conn().execute(q, args).fetchall()
        with Path(path).open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["key", "variant", "dep", "muni", "zona", "puesto",
                        "mesa", "expected_name", "http_status", "reason"])
            for r in rows:
                w.writerow([r[k] for k in r.keys()])
        return len(rows)
