"""Vote-worker logic tests — no AWS required (fakes for SQS + store).

The live SQS->Aurora pipeline is exercised by infra/smoke_worker.py; these lock in
the resilience invariants that must never regress: a message is deleted only after
its insert commits, malformed messages are skipped (not deleted -> DLQ via redrive),
and votes are batched by direction.
"""
from e14detector import vote_worker


class FakeSqs:
    def __init__(self, messages):
        self._messages = messages
        self.deleted = []

    def receive_message(self, **_):
        return {"Messages": self._messages}

    def delete_message_batch(self, QueueUrl, Entries):
        self.deleted.extend(Entries)
        return {}


class OkStore:
    def __init__(self):
        self.calls = []

    def record_votes_batch(self, strange, good):
        self.calls.append((list(strange), list(good)))


class RaisingStore:
    def record_votes_batch(self, strange, good):
        raise RuntimeError("DB down")


def _msg(mid, rh, body):
    return {"MessageId": mid, "ReceiptHandle": rh, "Body": body}


GOOD = '{"field_key":"k2","voter_token":"v2","direction":"good"}'
STRANGE = '{"field_key":"k1","voter_token":"v1","direction":"strange"}'
POISON = "not json"


def test_parse_valid_and_poison():
    assert vote_worker._parse({"Body": STRANGE}) == ("k1", "v1", "strange")
    assert vote_worker._parse({"Body": GOOD}) == ("k2", "v2", "good")
    assert vote_worker._parse({"Body": POISON}) is None
    assert vote_worker._parse({"Body": '{"field_key":"k"}'}) is None  # missing fields
    assert vote_worker._parse(
        {"Body": '{"field_key":"k","voter_token":"v","direction":"sideways"}'}
    ) is None  # bad direction


def test_drain_deletes_only_after_commit():
    msgs = [_msg("1", "r1", STRANGE), _msg("2", "r2", GOOD), _msg("3", "r3", POISON)]
    sqs = FakeSqs(msgs)
    store = OkStore()
    processed, poison = vote_worker.drain_once(sqs, "q", store)
    assert processed == 2 and poison == 1
    # only the 2 valid messages deleted; poison left for redrive -> DLQ
    assert len(sqs.deleted) == 2
    # batched by direction
    assert store.calls == [([("k1", "v1")], [("k2", "v2")])]


def test_drain_loses_nothing_when_db_down():
    msgs = [_msg("1", "r1", STRANGE), _msg("2", "r2", GOOD)]
    sqs = FakeSqs(msgs)
    raised = False
    try:
        vote_worker.drain_once(sqs, "q", RaisingStore())
    except RuntimeError:
        raised = True
    assert raised
    # nothing deleted -> SQS will redeliver every vote (zero loss)
    assert sqs.deleted == []


def test_drain_empty_receive_is_noop():
    sqs = FakeSqs([])
    assert vote_worker.drain_once(sqs, "q", OkStore()) == (0, 0)
    assert sqs.deleted == []
