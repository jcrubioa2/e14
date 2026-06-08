"""Postgres (Aurora Serverless v2) backend for the community vote store.

A drop-in replacement for the SQLite ``CommunityStore`` in [[community]], reached
over the **RDS Data API** (HTTPS + IAM — no VPC peering, no open DB port), so the
Fly web/worker processes can write durable, concurrent-safe votes without a
single-file SQLite writer behind a ``threading.Lock``.

Method names and signatures mirror ``CommunityStore`` exactly so ``webapp.py`` is
unchanged; ``community.make_store()`` picks this backend when the Aurora env vars
are set. Differences from SQLite, all intentional:

  * No process lock — Postgres handles concurrency. ``INSERT OR IGNORE`` becomes
    ``INSERT ... ON CONFLICT DO NOTHING``; "was it new?" comes from ``RETURNING``.
  * Each Data API ``execute_statement`` is its own implicit transaction; every
    method here is a single statement, so that is exactly the old per-call commit.
  * ``allow()`` (token bucket) is one atomic CTE statement instead of read-modify-
    write under a lock. Best-effort, same as before (no cross-machine lock exists).
"""
from __future__ import annotations

import json

# Pure helpers + config live in the SQLite module; reuse them verbatim so the two
# backends share one source of truth for identity/anti-fraud.
from .community import PollConfig  # noqa: F401  (re-exported for callers)
from .vote_aws import vote_client


def _typed(v):
    """Python value -> RDS Data API typed parameter value."""
    if v is None:
        return {"isNull": True}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"longValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _params(d: dict):
    return [{"name": k, "value": _typed(v)} for k, v in d.items()]


# The Aurora Data API does NOT support array parameters, so IN-clauses pass the key
# list as a JSON string and expand it in SQL. jsonb_array_elements_text is safe for
# any key content (unlike splitting a delimited string).
_IN_KEYS = "(SELECT jsonb_array_elements_text(:keys::jsonb))"


def _keys_param(values: list[str]):
    return [{"name": "keys", "value": {"stringValue": json.dumps(list(values))}}]


class PgCommunityStore:
    """Aurora-backed community store. Same surface as ``CommunityStore``."""

    def __init__(
        self,
        cluster_arn: str,
        secret_arn: str,
        database: str = "e14",
        region: str | None = None,  # accepted for symmetry; vote_client reads E14_VOTE_AWS_REGION
    ):
        self._client = vote_client("rds-data")
        self._base = dict(resourceArn=cluster_arn, secretArn=secret_arn, database=database)

    # -- Data API plumbing ---------------------------------------------------
    def _rows(self, sql: str, params: list | None = None) -> list[dict]:
        """Run a query, return rows as dicts (via the Data API JSON formatter)."""
        kw = dict(self._base, sql=sql, formatRecordsAs="JSON")
        if params:
            kw["parameters"] = params
        resp = self._client.execute_statement(**kw)
        return json.loads(resp.get("formattedRecords") or "[]")

    def _exec(self, sql: str, params: list | None = None) -> int:
        """Run a write, return number of rows affected."""
        kw = dict(self._base, sql=sql)
        if params:
            kw["parameters"] = params
        resp = self._client.execute_statement(**kw)
        return resp.get("numberOfRecordsUpdated", 0)

    def close(self) -> None:  # symmetry with CommunityStore; Data API is stateless
        pass

    # -- flags ---------------------------------------------------------------
    def record_flag(self, field_key: str, token: str) -> bool:
        rows = self._rows(
            "INSERT INTO flags (field_key, voter_token) VALUES (:fk, :tok) "
            "ON CONFLICT (field_key, voter_token) DO NOTHING RETURNING id",
            _params({"fk": field_key, "tok": token}),
        )
        return bool(rows)

    def record_votes_batch(
        self,
        strange: list[tuple[str, str]],
        good: list[tuple[str, str]],
    ) -> None:
        """Bulk-insert votes (used by the SQS worker): one Data API round-trip per
        direction instead of one per vote. ``strange``/``good`` are ``(field_key,
        voter_token)`` pairs. Idempotent via ON CONFLICT, so SQS redelivery is safe."""
        for table, rows in (("flags", strange), ("appeals", good)):
            if not rows:
                continue
            self._client.batch_execute_statement(
                **self._base,
                sql=(
                    f"INSERT INTO {table} (field_key, voter_token) VALUES (:fk, :tok) "
                    f"ON CONFLICT (field_key, voter_token) DO NOTHING"
                ),
                parameterSets=[_params({"fk": fk, "tok": tok}) for fk, tok in rows],
            )

    def _distinct_votes(self, field_key: str) -> int:
        rows = self._rows(
            "SELECT COUNT(DISTINCT voter_token) AS c FROM flags WHERE field_key=:fk",
            _params({"fk": field_key}),
        )
        return int(rows[0]["c"]) if rows else 0

    def distinct_votes(self, field_key: str) -> int:
        return self._distinct_votes(field_key)

    # -- appeal votes ("Se ve normal") --------------------------------------
    def record_appeal(self, field_key: str, token: str) -> bool:
        rows = self._rows(
            "INSERT INTO appeals (field_key, voter_token) VALUES (:fk, :tok) "
            "ON CONFLICT (field_key, voter_token) DO NOTHING RETURNING id",
            _params({"fk": field_key, "tok": token}),
        )
        return bool(rows)

    def _distinct_appeals(self, field_key: str) -> int:
        rows = self._rows(
            "SELECT COUNT(DISTINCT voter_token) AS c FROM appeals WHERE field_key=:fk",
            _params({"fk": field_key}),
        )
        return int(rows[0]["c"]) if rows else 0

    def distinct_appeals(self, field_key: str) -> int:
        return self._distinct_appeals(field_key)

    def delete_document(self, document_id: str) -> dict[str, int]:
        """Erase all community state for one acta (see ``CommunityStore.delete_document``).

        Field keys are ``document_id:page:row:section``; the LIKE escapes ``_``/``%``
        (document ids contain ``_``) so one acta never erases another. Returns per-table
        delete counts.
        """
        pattern = document_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%"
        out: dict[str, int] = {}
        for tbl in ("flags", "appeals", "field_state"):
            out[tbl] = self._exec(
                f"DELETE FROM {tbl} WHERE field_key LIKE :pat ESCAPE '\\'",
                _params({"pat": pattern}),
            )
        out["cid_index"] = self._exec(
            "DELETE FROM cid_index WHERE document_id=:did", _params({"did": document_id})
        )
        return out

    # -- adjudication state --------------------------------------------------
    def _upsert_state(self, field_key: str, **fields) -> None:
        # Ensure the row exists, then apply any column updates (updated_at always).
        self._exec(
            "INSERT INTO field_state (field_key) VALUES (:fk) ON CONFLICT DO NOTHING",
            _params({"fk": field_key}),
        )
        if fields:
            sets = ", ".join(f"{k}=:{k}" for k in fields)
            self._exec(
                f"UPDATE field_state SET {sets}, updated_at=now() WHERE field_key=:fk",
                _params({**fields, "fk": field_key}),
            )

    def try_claim_adjudication(self, field_key: str, threshold: int, rescale_step: int) -> int | None:
        votes = self._distinct_votes(field_key)
        rows = self._rows(
            "SELECT vlm_state, last_adjudicated_votes FROM field_state WHERE field_key=:fk",
            _params({"fk": field_key}),
        )
        state = rows[0]["vlm_state"] if rows else "NONE"
        last = int(rows[0]["last_adjudicated_votes"]) if rows else 0
        if state in ("PENDING", "STRANGE"):
            return None
        if state == "NONE" and votes < threshold:
            return None
        if state == "CLEAN" and votes < last + rescale_step:
            return None
        self._upsert_state(field_key, vlm_state="PENDING")
        return votes

    def record_verdict(
        self, field_key: str, strange: bool, votes_at_call: int, image_hash: str | None = None
    ) -> None:
        self._upsert_state(
            field_key,
            vlm_state="STRANGE" if strange else "CLEAN",
            published=1 if strange else 0,
            last_adjudicated_votes=votes_at_call,
            image_hash=image_hash,
        )

    def release_pending(self, field_key: str) -> None:
        self._exec(
            "UPDATE field_state SET vlm_state='NONE', updated_at=now() "
            "WHERE field_key=:fk AND vlm_state='PENDING'",
            _params({"fk": field_key}),
        )

    def try_claim_appeal(self, field_key: str, threshold: int, rescale_step: int) -> int | None:
        votes = self._distinct_appeals(field_key)
        rows = self._rows(
            "SELECT appeal_state, last_appealed_votes, appeal_cleared "
            "FROM field_state WHERE field_key=:fk",
            _params({"fk": field_key}),
        )
        astate = rows[0]["appeal_state"] if rows else "NONE"
        last = int(rows[0]["last_appealed_votes"]) if rows else 0
        cleared = int(rows[0]["appeal_cleared"]) if rows else 0
        if astate == "PENDING" or cleared:
            return None
        if last == 0 and votes < threshold:
            return None
        if last > 0 and votes < last + rescale_step:
            return None
        self._upsert_state(field_key, appeal_state="PENDING")
        return votes

    def record_appeal_verdict(
        self, field_key: str, cleared: bool, votes_at_call: int, image_hash: str | None = None
    ) -> None:
        fields = dict(
            appeal_state="NONE",
            last_appealed_votes=votes_at_call,
            appeal_cleared=1 if cleared else 0,
        )
        if image_hash:
            fields["image_hash"] = image_hash
        if cleared:
            fields["published"] = 0
            fields["vlm_state"] = "CLEAN"
        self._upsert_state(field_key, **fields)

    def release_appeal(self, field_key: str) -> None:
        self._exec(
            "UPDATE field_state SET appeal_state='NONE', updated_at=now() "
            "WHERE field_key=:fk AND appeal_state='PENDING'",
            _params({"fk": field_key}),
        )

    def cleared_among(self, field_keys: list[str]) -> set[str]:
        if not field_keys:
            return set()
        rows = self._rows(
            f"SELECT field_key FROM field_state WHERE appeal_cleared=1 AND field_key IN {_IN_KEYS}",
            _keys_param(field_keys),
        )
        return {r["field_key"] for r in rows}

    def cleared_keys(self) -> list[str]:
        rows = self._rows("SELECT field_key FROM field_state WHERE appeal_cleared=1")
        return [r["field_key"] for r in rows]

    def state_of(self, field_key: str) -> dict | None:
        rows = self._rows(
            "SELECT * FROM field_state WHERE field_key=:fk", _params({"fk": field_key})
        )
        return rows[0] if rows else None

    def high_voted_fields(self, threshold: int) -> set[str]:
        rows = self._rows(
            "SELECT field_key FROM flags GROUP BY field_key "
            "HAVING COUNT(DISTINCT voter_token) >= :t",
            _params({"t": threshold}),
        )
        return {r["field_key"] for r in rows}

    def acta_popularity(self) -> dict[str, int]:
        # Distinct voters per acta. The document id is the field_key minus its last
        # 3 ':'-parts (page:row:section) -> mirrors community.py's rsplit(":", 3)[0].
        rows = self._rows(
            """
            SELECT doc, COUNT(DISTINCT voter_token) AS n FROM (
                SELECT array_to_string(
                           (string_to_array(field_key, ':'))
                               [1 : array_length(string_to_array(field_key, ':'), 1) - 3],
                           ':') AS doc,
                       voter_token
                FROM flags
            ) sub
            GROUP BY doc
            """
        )
        return {r["doc"]: int(r["n"]) for r in rows}

    def total_reviews(self) -> int:
        # Total mesas reviewed = distinct (person, mesa) across both vote directions (an Enviar
        # votes on a mesa's casillas). doc = field_key minus its last 3 ':'-parts, as in
        # acta_popularity; UNION ALL folds flags + appeals so all-"se ve bien" mesas count too.
        rows = self._rows(
            """
            SELECT COUNT(*) AS n FROM (
                SELECT DISTINCT voter_token,
                       array_to_string(
                           (string_to_array(field_key, ':'))
                               [1 : array_length(string_to_array(field_key, ':'), 1) - 3],
                           ':') AS doc
                FROM (SELECT voter_token, field_key FROM flags
                      UNION ALL
                      SELECT voter_token, field_key FROM appeals) u
            ) t
            """
        )
        return int(rows[0]["n"]) if rows else 0

    def reviewed_actas(self) -> set[str]:
        # Doc ids reviewed (flagged OR marked good) — the 'reviewed' denominator for the /reportes
        # map. doc = field_key minus its last 3 ':'-parts, as in acta_popularity; UNION ALL folds
        # flags + appeals so all-"se ve bien" mesas count too.
        rows = self._rows(
            """
            SELECT DISTINCT array_to_string(
                       (string_to_array(field_key, ':'))
                           [1 : array_length(string_to_array(field_key, ':'), 1) - 3],
                       ':') AS doc
            FROM (SELECT field_key FROM flags
                  UNION ALL
                  SELECT field_key FROM appeals) u
            """
        )
        return {r["doc"] for r in rows}

    def review_counts(self) -> dict[str, int]:
        # Distinct reviewers per acta (coverage intensity for the admin board). Mirrors
        # total_reviews but grouped by doc: COUNT(DISTINCT voter) per mesa across both vote
        # directions. doc = field_key minus its last 3 ':'-parts, as in reviewed_actas.
        rows = self._rows(
            """
            SELECT doc, COUNT(DISTINCT voter_token) AS n FROM (
                SELECT voter_token,
                       array_to_string(
                           (string_to_array(field_key, ':'))
                               [1 : array_length(string_to_array(field_key, ':'), 1) - 3],
                           ':') AS doc
                FROM (SELECT voter_token, field_key FROM flags
                      UNION ALL
                      SELECT voter_token, field_key FROM appeals) u
            ) sub
            GROUP BY doc
            """
        )
        return {r["doc"]: int(r["n"]) for r in rows}

    def published_keys(self) -> list[str]:
        rows = self._rows("SELECT field_key FROM field_state WHERE published=1")
        return [r["field_key"] for r in rows]

    def published_among(self, field_keys: list[str]) -> set[str]:
        if not field_keys:
            return set()
        rows = self._rows(
            f"SELECT field_key FROM field_state WHERE published=1 AND field_key IN {_IN_KEYS}",
            _keys_param(field_keys),
        )
        return {r["field_key"] for r in rows}

    def admin_overview(self) -> list[dict]:
        votes = {r["field_key"]: int(r["n"]) for r in self._rows(
            "SELECT field_key, COUNT(DISTINCT voter_token) n FROM flags GROUP BY field_key")}
        appeals = {r["field_key"]: int(r["n"]) for r in self._rows(
            "SELECT field_key, COUNT(DISTINCT voter_token) n FROM appeals GROUP BY field_key")}
        states = {r["field_key"]: r for r in self._rows("SELECT * FROM field_state")}
        rows = []
        for k in set(votes) | set(states) | set(appeals):
            s = states.get(k, {})
            rows.append({
                "field_key": k,
                "votes": votes.get(k, 0),
                "appeals": appeals.get(k, 0),
                "vlm_state": s.get("vlm_state", "NONE"),
                "published": bool(s.get("published")),
                "triggered_at_votes": s.get("last_adjudicated_votes", 0),
                "appeal_state": s.get("appeal_state", "NONE"),
                "appeal_cleared": bool(s.get("appeal_cleared")),
                "image_hash": (s.get("image_hash") or "")[:12],
                "updated_at": s.get("updated_at"),
            })
        rows.sort(key=lambda r: (-r["votes"], r["field_key"]))
        return rows

    def pending_among(self, field_keys: list[str]) -> set[str]:
        if not field_keys:
            return set()
        rows = self._rows(
            f"SELECT field_key FROM field_state WHERE vlm_state='PENDING' AND field_key IN {_IN_KEYS}",
            _keys_param(field_keys),
        )
        return {r["field_key"] for r in rows}

    # -- rate limit ----------------------------------------------------------
    def allow(self, token: str, refill_per_min: float, bucket: float) -> bool:
        """Atomic token-bucket rate limit in one statement (no app-side lock).

        Refills from the stored timestamp to now, decrements iff >= 1 token, and
        upserts the result. Best-effort under extreme same-token concurrency.
        """
        rows = self._rows(
            """
            WITH cur AS (
                SELECT tokens, updated_at FROM rate_buckets WHERE voter_token = :tok
            ),
            calc AS (
                SELECT LEAST(
                    :bucket,
                    COALESCE((SELECT tokens FROM cur), :bucket)
                    + (:now - COALESCE((SELECT updated_at FROM cur), :now)) * (:refill / 60.0)
                ) AS refilled
            ),
            decision AS (
                SELECT refilled, (refilled >= 1.0) AS allowed FROM calc
            ),
            upsert AS (
                INSERT INTO rate_buckets (voter_token, tokens, updated_at)
                SELECT :tok, refilled - CASE WHEN allowed THEN 1.0 ELSE 0.0 END, :now
                  FROM decision
                ON CONFLICT (voter_token) DO UPDATE
                    SET tokens = EXCLUDED.tokens, updated_at = EXCLUDED.updated_at
                RETURNING 1
            )
            SELECT allowed FROM decision
            """,
            _params({"tok": token, "bucket": float(bucket), "refill": float(refill_per_min),
                     "now": __import__("time").time()}),
        )
        return bool(rows and rows[0]["allowed"])

    # -- anonymized crop ids (swipe feed) -----------------------------------
    def register_cid(self, cid: str, field_key: str, crop_rel: str, document_id: str) -> None:
        self._exec(
            "INSERT INTO cid_index (cid, field_key, crop_rel, document_id) "
            "VALUES (:cid, :fk, :rel, :doc) ON CONFLICT (cid) DO NOTHING",
            _params({"cid": cid, "fk": field_key, "rel": crop_rel, "doc": document_id}),
        )

    def register_cids(self, rows: list[tuple[str, str, str, str]]) -> None:
        if not rows:
            return
        param_sets = [
            _params({"cid": c, "fk": fk, "rel": rel, "doc": doc})
            for (c, fk, rel, doc) in rows
        ]
        self._client.batch_execute_statement(
            **self._base,
            sql=(
                "INSERT INTO cid_index (cid, field_key, crop_rel, document_id) "
                "VALUES (:cid, :fk, :rel, :doc) ON CONFLICT (cid) DO NOTHING"
            ),
            parameterSets=param_sets,
        )

    def resolve_cid(self, cid: str) -> dict | None:
        rows = self._rows(
            "SELECT field_key, crop_rel, document_id FROM cid_index WHERE cid=:cid",
            _params({"cid": cid}),
        )
        return rows[0] if rows else None

    # -- public tallies ------------------------------------------------------
    def strange_count(self, field_key: str) -> int:
        return self.distinct_votes(field_key)

    def good_count(self, field_key: str) -> int:
        return self.distinct_appeals(field_key)

    def counts_among(self, field_keys: list[str]) -> dict[str, dict[str, int]]:
        out = {k: {"good": 0, "strange": 0} for k in field_keys}
        if not field_keys:
            return out
        arr = _keys_param(field_keys)
        for r in self._rows(
            f"SELECT field_key, COUNT(DISTINCT voter_token) n FROM flags "
            f"WHERE field_key IN {_IN_KEYS} GROUP BY field_key", arr):
            out[r["field_key"]]["strange"] = int(r["n"])
        for r in self._rows(
            f"SELECT field_key, COUNT(DISTINCT voter_token) n FROM appeals "
            f"WHERE field_key IN {_IN_KEYS} GROUP BY field_key", arr):
            out[r["field_key"]]["good"] = int(r["n"])
        return out

    def hot_crops(self, limit: int) -> list[dict]:
        rows = self._rows(
            """
            SELECT field_key, SUM(strange) AS strange, SUM(good) AS good
            FROM (
                SELECT field_key, COUNT(DISTINCT voter_token) AS strange, 0 AS good
                  FROM flags GROUP BY field_key
                UNION ALL
                SELECT field_key, 0 AS strange, COUNT(DISTINCT voter_token) AS good
                  FROM appeals GROUP BY field_key
            ) t
            GROUP BY field_key
            ORDER BY SUM(strange) DESC, (SUM(strange) + SUM(good)) DESC, field_key
            LIMIT :lim
            """,
            _params({"lim": limit}),
        )
        return [{"field_key": r["field_key"], "good": int(r["good"] or 0),
                 "strange": int(r["strange"] or 0)} for r in rows]
