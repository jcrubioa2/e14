"""AWS Lambda: SQS -> Aurora vote drain (replaces the Fly ``vote_worker`` process).

Triggered by the ``e14-vote-events`` queue through an event source mapping with
partial batch responses enabled. For each SQS batch this:

  * parses + validates every message (the shape ``VotePublisher`` emits),
  * bulk-inserts the votes into ``flags``/``appeals`` via the RDS Data API
    (idempotent ``ON CONFLICT DO NOTHING`` — mirrors
    ``community_pg.PgCommunityStore.record_votes_batch`` verbatim), then
  * returns ``batchItemFailures`` so SQS redelivers only what we did NOT durably
    commit.

Failure semantics (parity with ``vote_worker.drain_once``):

  * **DB / AWS error** -> report *all* records as failures; SQS redelivers the whole
    batch; nothing is lost; the idempotent insert makes the retry safe.
  * **Malformed message** -> report as a failure; after ``maxReceiveCount`` it lands
    in the DLQ (same as the worker leaving poison undeleted so redrive moves it).

Why this is simpler than the Fly worker:
  * No long-poll loop / SIGTERM handling — the event source mapping owns receive +
    delete and only invokes us when work exists (idle cost ~= $0).
  * No ``E14_VOTE_AWS_*`` juggling — that only existed to stop boto3 picking up Fly's
    Tigris ``AWS_*`` keys. The Lambda execution role supplies IAM natively.
  * ``boto3`` ships in the Lambda runtime, so the deployment package is just this file.

Env: ``AURORA_CLUSTER_ARN``, ``AURORA_SECRET_ARN``, ``AURORA_DATABASE`` (default ``e14``).
"""
from __future__ import annotations

import json
import os

import boto3

_VALID = {"good", "strange"}

# Created lazily on the first invocation, then warm across invocations on a reused execution
# environment. Lazy (not at import) so the module imports with no AWS region/creds — e.g. in tests,
# which drive ``process`` directly with a fake Data API client.
_client = None
_base_cache: dict | None = None


def _get_client():
    """The RDS Data API client, built once on first use and reused on warm invocations."""
    global _client
    if _client is None:
        _client = boto3.client("rds-data")
    return _client


def _base() -> dict:
    """The constant Data API call kwargs (resource/secret/database), read once."""
    global _base_cache
    if _base_cache is None:
        _base_cache = {
            "resourceArn": os.environ["AURORA_CLUSTER_ARN"],
            "secretArn": os.environ["AURORA_SECRET_ARN"],
            "database": os.environ.get("AURORA_DATABASE", "e14"),
        }
    return _base_cache


def _parse(record: dict) -> tuple[str, str, str] | None:
    """(field_key, voter_token, direction) or None if the message is malformed."""
    try:
        b = json.loads(record["body"])
        fk, tok, direction = b["field_key"], b["voter_token"], b["direction"]
    except (KeyError, ValueError, TypeError):
        return None
    if not fk or not tok or direction not in _VALID:
        return None
    return fk, tok, direction


def _insert(client, base: dict, strange: list, good: list) -> None:
    """Bulk-insert votes, one Data API round-trip per direction. Idempotent.

    Kept byte-for-byte in step with ``community_pg.PgCommunityStore.record_votes_batch``
    so the Lambda and the (legacy) Fly worker write identical rows.
    """
    for table, rows in (("flags", strange), ("appeals", good)):
        if not rows:
            continue
        client.batch_execute_statement(
            **base,
            sql=(
                f"INSERT INTO {table} (field_key, voter_token) VALUES (:fk, :tok) "
                f"ON CONFLICT (field_key, voter_token) DO NOTHING"
            ),
            parameterSets=[
                [
                    {"name": "fk", "value": {"stringValue": fk}},
                    {"name": "tok", "value": {"stringValue": tok}},
                ]
                for fk, tok in rows
            ],
        )


def process(event: dict, client, base: dict) -> dict:
    """Drain one SQS batch. Returns the partial-batch-failure response.

    ``client``/``base`` are injected so this is unit-testable without AWS.
    """
    records = event.get("Records", [])
    strange: list[tuple[str, str]] = []
    good: list[tuple[str, str]] = []
    valid_ids: list[str] = []
    poison_ids: list[str] = []

    for r in records:
        parsed = _parse(r)
        if parsed is None:
            poison_ids.append(r["messageId"])  # -> retried -> DLQ after maxReceiveCount
            continue
        fk, tok, direction = parsed
        (strange if direction == "strange" else good).append((fk, tok))
        valid_ids.append(r["messageId"])

    try:
        _insert(client, base, strange, good)
    except Exception:
        # Nothing committed durably -> redeliver the whole batch (valid + poison).
        # The ON CONFLICT insert makes the eventual retry safe; no vote is lost.
        failed = valid_ids + poison_ids
        return {"batchItemFailures": [{"itemIdentifier": i} for i in failed]}

    # Valid votes are committed; only the poison goes back (and on toward the DLQ).
    return {"batchItemFailures": [{"itemIdentifier": i} for i in poison_ids]}


def handler(event: dict, context=None) -> dict:
    return process(event, _get_client(), _base())
