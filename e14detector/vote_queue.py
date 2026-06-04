"""SQS publisher for the durable vote path.

The web request validates a vote (rate-limit, bot screen, Turnstile, cid -> field_key)
and then **enqueues** it instead of writing to the DB synchronously. SQS absorbs the
vote so it survives even if the worker or Aurora is momentarily down — the whole point
of the AWS migration. The worker (`vote_worker.py`) drains the queue into Postgres.

Message body: ``{"field_key", "voter_token", "direction": "good"|"strange", "ts"}``.
Dedup is enforced downstream by the UNIQUE(field_key, voter_token) constraint, so
at-least-once SQS redelivery is safe (idempotent).
"""
from __future__ import annotations

import json
import os
import time

from .vote_aws import vote_client


class VotePublisher:
    def __init__(self, queue_url: str):
        self._sqs = vote_client("sqs")
        self._url = queue_url

    def publish(self, field_key: str, voter_token: str, direction: str) -> None:
        self._sqs.send_message(
            QueueUrl=self._url,
            MessageBody=json.dumps(
                {
                    "field_key": field_key,
                    "voter_token": voter_token,
                    "direction": direction,
                    "ts": time.time(),
                }
            ),
        )

    def queue_depth(self) -> dict[str, int] | None:
        """Approximate queue backlog for the admin health board: messages waiting and
        in-flight. Returns None on any failure (the dashboard then shows the probe as down)."""
        try:
            attrs = self._sqs.get_queue_attributes(
                QueueUrl=self._url,
                AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
            ).get("Attributes", {})
            return {
                "waiting": int(attrs.get("ApproximateNumberOfMessages", 0)),
                "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
            }
        except Exception:  # noqa: BLE001 — health probe must never raise
            return None


def make_publisher() -> VotePublisher | None:
    """A publisher when ``SQS_QUEUE_URL`` is configured, else ``None`` (the app then
    falls back to the synchronous DB write — local dev, tests, single-machine mode)."""
    url = os.environ.get("SQS_QUEUE_URL")
    return VotePublisher(url) if url else None
