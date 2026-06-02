"""Community-flag store for the public E-14 report.

The public report lets anyone flag a candidate crop as anomalous. Crowd pressure
is *only a trigger*: when distinct flags cross a threshold we ask the VLM for a
second opinion, and the VLM — not the crowd — decides what is published as
"strange". This bounds the worst case (vote-stuffing can nominate a crop for
review but can never directly publish a false verdict).

Settle policy = **clean is re-eligible**: a VLM "clean" verdict un-publishes but
does not immunize the crop. If distinct votes keep climbing by another
``rescale_step`` it is re-adjudicated, so one flaky "clean" call (the VLM is
provably non-deterministic on the fused-dot fraud class) cannot bury a real
anomaly forever. A "strange" verdict is terminal and published.

This store owns its own writable SQLite file, kept separate from the read-only
results DB so re-running the detector never wipes votes and write contention
never touches the report's read path. Votes are keyed by a **stable** field key
(``document:page:row:section``), not ``vote_fields.id`` (which is AUTOINCREMENT and
reassigned on every re-run).
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_key TEXT NOT NULL,
    voter_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(field_key, voter_token)
);
CREATE INDEX IF NOT EXISTS idx_flags_field ON flags(field_key);

CREATE TABLE IF NOT EXISTS field_state (
    field_key TEXT PRIMARY KEY,
    vlm_state TEXT NOT NULL DEFAULT 'NONE',   -- NONE | PENDING | CLEAN | STRANGE
    last_adjudicated_votes INTEGER NOT NULL DEFAULT 0,
    published INTEGER NOT NULL DEFAULT 0,
    image_hash TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_buckets (
    voter_token TEXT PRIMARY KEY,
    tokens REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def field_key_of(document_id: str, page_number: int, row_number: int, section: str | None) -> str:
    """Stable identity for a vote field; survives detector re-runs."""
    return f"{document_id}:{page_number}:{row_number}:{section or ''}"


def voter_token(salt: str, client_ip: str, session_id: str, day: str | None = None) -> str:
    """Daily-rotating, privacy-preserving voter identity.

    Hashing in a per-day bucket means no raw IP is stored and tokens naturally
    expire, bounding the rate-bucket table. Best-effort only (true one-person-one-
    vote needs accounts) — acceptable because crossing the threshold merely
    triggers VLM adjudication, never publication.
    """
    day = day or date.today().isoformat()
    raw = f"{salt}|{day}|{client_ip}|{session_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verify_turnstile(secret: str, token: str | None, remote_ip: str | None = None) -> bool:
    """Verify a Cloudflare Turnstile token. Empty secret => skip (local/dev)."""
    if not secret:
        return True
    if not token:
        return False
    try:
        import requests

        resp = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": secret, "response": token, "remoteip": remote_ip or ""},
            timeout=10,
        )
        return bool(resp.json().get("success"))
    except Exception:
        return False


@dataclass(frozen=True)
class PollConfig:
    """Tunables for the community poll; defaults come from ``config`` / env."""

    threshold: int = 5
    rescale_step: int = 5
    rate_refill_per_min: float = 10.0
    rate_bucket: float = 20.0
    turnstile_secret: str = ""
    turnstile_sitekey: str = ""
    voter_salt: str = "e14-dev-salt"

    @classmethod
    def from_config(cls) -> "PollConfig":
        return cls(
            threshold=config.POLL_THRESHOLD,
            rescale_step=config.POLL_RESCALE_STEP,
            rate_refill_per_min=config.RATE_REFILL_PER_MIN,
            rate_bucket=config.RATE_BUCKET,
            turnstile_secret=config.TURNSTILE_SECRET,
            turnstile_sitekey=config.TURNSTILE_SITEKEY,
            voter_salt=config.VOTER_SALT,
        )


class CommunityStore:
    """Writable store for community flags + adjudication state.

    A single process-wide lock serializes all writes/decisions so the background
    VLM-adjudication threads and the request handlers never race (SQLite would
    otherwise need careful transaction juggling). The connection is opened with
    ``check_same_thread=False`` precisely because adjudication runs on a worker
    thread (``asyncio.to_thread``).
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- flags ---------------------------------------------------------------
    def record_flag(self, field_key: str, token: str) -> bool:
        """Insert one vote; returns True only if it was new (dedup per identity)."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO flags (field_key, voter_token, created_at) VALUES (?,?,?)",
                (field_key, token, _now_iso()),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def _distinct_votes(self, field_key: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(DISTINCT voter_token) c FROM flags WHERE field_key=?",
            (field_key,),
        ).fetchone()["c"]

    def distinct_votes(self, field_key: str) -> int:
        with self._lock:
            return self._distinct_votes(field_key)

    # -- adjudication state --------------------------------------------------
    def _upsert_state(self, field_key: str, **fields) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO field_state (field_key, updated_at) VALUES (?, ?)",
            (field_key, _now_iso()),
        )
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(
                f"UPDATE field_state SET {cols}, updated_at=? WHERE field_key=?",
                (*fields.values(), _now_iso(), field_key),
            )

    def try_claim_adjudication(
        self, field_key: str, threshold: int, rescale_step: int
    ) -> int | None:
        """Atomically decide whether to fire a VLM review and, if so, claim it.

        Returns the distinct-vote count at claim time (caller fires the VLM with
        it), or ``None`` if no review should run now. Setting ``PENDING`` under the
        lock de-dups concurrent flags so the VLM is called at most once per wave.
        """
        with self._lock:
            votes = self._distinct_votes(field_key)
            row = self.conn.execute(
                "SELECT vlm_state, last_adjudicated_votes FROM field_state WHERE field_key=?",
                (field_key,),
            ).fetchone()
            state = row["vlm_state"] if row else "NONE"
            last = row["last_adjudicated_votes"] if row else 0
            if state in ("PENDING", "STRANGE"):
                return None  # in flight, or terminal
            if state == "NONE" and votes < threshold:
                return None
            if state == "CLEAN" and votes < last + rescale_step:
                return None  # re-eligible only after another step of fresh votes
            self._upsert_state(field_key, vlm_state="PENDING")
            self.conn.commit()
            return votes

    def record_verdict(
        self, field_key: str, strange: bool, votes_at_call: int, image_hash: str | None = None
    ) -> None:
        with self._lock:
            self._upsert_state(
                field_key,
                vlm_state="STRANGE" if strange else "CLEAN",
                published=1 if strange else 0,
                last_adjudicated_votes=votes_at_call,
                image_hash=image_hash,
            )
            self.conn.commit()

    def release_pending(self, field_key: str) -> None:
        """Roll a PENDING claim back to NONE after a failed VLM call, so it can retry."""
        with self._lock:
            self.conn.execute(
                "UPDATE field_state SET vlm_state='NONE', updated_at=? "
                "WHERE field_key=? AND vlm_state='PENDING'",
                (_now_iso(), field_key),
            )
            self.conn.commit()

    def state_of(self, field_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM field_state WHERE field_key=?", (field_key,)
            ).fetchone()

    def published_keys(self) -> list[str]:
        """All field keys currently published as strange (few — only confirmed ones)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT field_key FROM field_state WHERE published=1"
            ).fetchall()
        return [r["field_key"] for r in rows]

    def published_among(self, field_keys: list[str]) -> set[str]:
        """Subset of the given keys currently published as strange (for templates)."""
        if not field_keys:
            return set()
        with self._lock:
            placeholders = ",".join("?" for _ in field_keys)
            rows = self.conn.execute(
                f"SELECT field_key FROM field_state WHERE published=1 "
                f"AND field_key IN ({placeholders})",
                field_keys,
            ).fetchall()
        return {r["field_key"] for r in rows}

    # -- rate limit ----------------------------------------------------------
    def allow(self, token: str, refill_per_min: float, bucket: float) -> bool:
        """Token-bucket rate limit per voter. Returns False when exhausted."""
        now = time.time()
        with self._lock:
            row = self.conn.execute(
                "SELECT tokens, updated_at FROM rate_buckets WHERE voter_token=?",
                (token,),
            ).fetchone()
            if row is None:
                tokens = bucket
                last = now
            else:
                tokens, last = row["tokens"], row["updated_at"]
            tokens = min(bucket, tokens + (now - last) * (refill_per_min / 60.0))
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self.conn.execute(
                "INSERT INTO rate_buckets (voter_token, tokens, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(voter_token) DO UPDATE SET tokens=excluded.tokens, updated_at=excluded.updated_at",
                (token, tokens, now),
            )
            self.conn.commit()
            return allowed
