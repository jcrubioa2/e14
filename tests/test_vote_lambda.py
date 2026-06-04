"""Lambda vote-drain tests — no AWS required (fake RDS Data API client).

The Lambda replaces the Fly worker, so it must keep the same resilience invariants
(see tests/test_vote_worker.py): valid votes are batched by direction and only
reported as committed when the insert succeeds, a DB error redelivers the whole
batch (nothing lost), and malformed messages are reported as failures so SQS redrive
moves them to the DLQ. The partial-batch response is the Lambda equivalent of the
worker's "delete only what committed".
"""
import importlib.util
import json
from pathlib import Path

# infra/lambda/ is a deployment asset, not an importable package — load by path.
_HANDLER_PATH = Path(__file__).resolve().parents[1] / "infra" / "lambda" / "handler.py"
_spec = importlib.util.spec_from_file_location("vote_lambda_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

_BASE = {"resourceArn": "arn:cluster", "secretArn": "arn:secret", "database": "e14"}


class FakeDataApi:
    """Records batch_execute_statement calls; optionally raises to simulate DB down."""

    def __init__(self, raise_on_call=False):
        self.calls = []
        self._raise = raise_on_call

    def batch_execute_statement(self, sql, parameterSets, **base):
        if self._raise:
            raise RuntimeError("DB down")
        self.calls.append((sql, parameterSets))
        return {}


def _record(mid, body):
    return {"messageId": mid, "body": body}


def _vote(fk, tok, direction):
    return json.dumps({"field_key": fk, "voter_token": tok, "direction": direction})


def _event(*records):
    return {"Records": list(records)}


def _table(sql: str) -> str:
    # "INSERT INTO flags (...)" -> "flags"
    return sql.split("INSERT INTO ", 1)[1].split(" ", 1)[0]


def test_valid_votes_batched_by_direction_and_no_failures():
    client = FakeDataApi()
    event = _event(
        _record("m1", _vote("fk1", "tokA", "strange")),
        _record("m2", _vote("fk1", "tokB", "good")),
        _record("m3", _vote("fk2", "tokC", "strange")),
    )
    resp = handler.process(event, client, _BASE)

    assert resp == {"batchItemFailures": []}
    by_table = {_table(sql): params for sql, params in client.calls}
    assert set(by_table) == {"flags", "appeals"}
    assert len(by_table["flags"]) == 2     # the two "strange" votes
    assert len(by_table["appeals"]) == 1   # the one "good" vote


def test_db_error_reports_all_records_for_redelivery():
    client = FakeDataApi(raise_on_call=True)
    event = _event(
        _record("m1", _vote("fk1", "tokA", "strange")),
        _record("m2", _vote("fk2", "tokB", "good")),
    )
    resp = handler.process(event, client, _BASE)

    failed = {f["itemIdentifier"] for f in resp["batchItemFailures"]}
    assert failed == {"m1", "m2"}  # whole batch retried; nothing deleted/committed


def test_malformed_message_goes_to_failures_but_valid_ones_commit():
    client = FakeDataApi()
    event = _event(
        _record("ok", _vote("fk1", "tokA", "strange")),
        _record("bad_json", "{not json"),
        _record("bad_dir", _vote("fk2", "tokB", "sideways")),
        _record("missing", json.dumps({"field_key": "fk3"})),
    )
    resp = handler.process(event, client, _BASE)

    failed = {f["itemIdentifier"] for f in resp["batchItemFailures"]}
    assert failed == {"bad_json", "bad_dir", "missing"}  # -> DLQ via redrive
    # The one valid vote still committed.
    assert sum(len(p) for _, p in client.calls) == 1


def test_empty_batch_is_a_noop():
    client = FakeDataApi()
    resp = handler.process(_event(), client, _BASE)
    assert resp == {"batchItemFailures": []}
    assert client.calls == []
