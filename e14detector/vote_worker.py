"""SQS -> Postgres vote drain worker.

Long-polls the vote queue, bulk-inserts each batch into ``flags``/``appeals``
(idempotent ON CONFLICT, so at-least-once redelivery is safe), then deletes the
processed messages. Resilience guarantees:

  * **No vote lost.** A message is deleted only *after* its insert commits. If the
    DB is down the batch insert raises, nothing is deleted, and SQS redelivers.
  * **Poison messages** (malformed JSON / bad direction) are never deletable, so
    SQS redrive moves them to the DLQ after ``maxReceiveCount`` — they don't wedge
    the queue.

Run as a Fly process: ``python -m e14detector.vote_worker``.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time

from .community import make_store
from .vote_aws import vote_client

log = logging.getLogger("e14.vote_worker")

WAIT_SECONDS = 20          # SQS long-poll (max); cheap and low-latency
MAX_MESSAGES = 10          # SQS receive cap per call
_VALID = {"good", "strange"}


class _Stopper:
    """Flips on SIGTERM/SIGINT so the loop exits cleanly between polls (Fly sends SIGTERM)."""

    def __init__(self):
        self.stop = False
        signal.signal(signal.SIGTERM, self._set)
        signal.signal(signal.SIGINT, self._set)

    def _set(self, *_):
        self.stop = True


def _parse(msg: dict) -> tuple[str, str, str] | None:
    """(field_key, voter_token, direction) or None if the message is malformed."""
    try:
        b = json.loads(msg["Body"])
        fk, tok, direction = b["field_key"], b["voter_token"], b["direction"]
    except (KeyError, ValueError, TypeError):
        return None
    if not fk or not tok or direction not in _VALID:
        return None
    return fk, tok, direction


def drain_once(sqs, queue_url: str, store) -> tuple[int, int]:
    """Process one receive batch. Returns (processed, skipped_poison)."""
    resp = sqs.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=MAX_MESSAGES, WaitTimeSeconds=WAIT_SECONDS
    )
    messages = resp.get("Messages", [])
    if not messages:
        return 0, 0

    strange: list[tuple[str, str]] = []
    good: list[tuple[str, str]] = []
    deletable: list[dict] = []  # only messages we successfully accounted for
    poison = 0
    for m in messages:
        parsed = _parse(m)
        if parsed is None:
            poison += 1
            log.warning("dropping malformed vote message (-> DLQ via redrive): %s", m.get("MessageId"))
            continue  # leave undeleted; SQS redrive -> DLQ
        fk, tok, direction = parsed
        (strange if direction == "strange" else good).append((fk, tok))
        deletable.append(m)

    if deletable:
        # If this raises (DB down), we delete nothing -> SQS redelivers the whole batch.
        store.record_votes_batch(strange, good)
        sqs.delete_message_batch(
            QueueUrl=queue_url,
            Entries=[{"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]}
                     for i, m in enumerate(deletable)],
        )
    return len(deletable), poison


def run(queue_url: str | None = None, store=None) -> None:
    queue_url = queue_url or os.environ["SQS_QUEUE_URL"]
    sqs = vote_client("sqs")
    store = store or make_store()
    stopper = _Stopper()
    processed = poison = 0
    last_log = time.time()
    log.info("vote_worker started; draining %s", queue_url)

    while not stopper.stop:
        try:
            p, x = drain_once(sqs, queue_url, store)
            processed += p
            poison += x
        except Exception:
            # Transient AWS/DB error: log and back off briefly; messages stay on the
            # queue (nothing deleted on the failing path), so no vote is lost.
            log.exception("drain batch failed; retrying")
            time.sleep(2)
        if time.time() - last_log >= 30:
            log.info("vote_worker progress: processed=%d poison=%d", processed, poison)
            last_log = time.time()

    log.info("vote_worker stopping: processed=%d poison=%d", processed, poison)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("E14_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()


if __name__ == "__main__":
    main()
