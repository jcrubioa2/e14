import sqlite3

import pytest


@pytest.fixture(autouse=True)
def _fast_throwaway_sqlite(monkeypatch):
    """Make the per-test SQLite DBs skip durable fsync.

    Every test builds a fresh ``DetectorStore`` + ``Community`` DB from scratch, and each
    ``executescript``/``commit`` fsyncs to disk — that dominated the suite (~9s of ~20s,
    and far worse under concurrent disk load). These DBs are throwaway, so durability buys
    nothing here: ``synchronous=OFF`` skips the fsyncs and ``journal_mode=MEMORY`` keeps the
    rollback journal off disk. Test-only — production ``sqlite3.connect`` calls are untouched.
    """
    real_connect = sqlite3.connect

    def fast_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        # Best-effort: read-only connections (the webapp opens DBs with ?mode=ro) reject
        # journal/sync writes with "disk I/O error" — those do no fsync anyway, so skip them.
        for pragma in ("PRAGMA synchronous=OFF", "PRAGMA journal_mode=MEMORY"):
            try:
                conn.execute(pragma)
            except sqlite3.OperationalError:
                pass
        return conn

    monkeypatch.setattr(sqlite3, "connect", fast_connect)
