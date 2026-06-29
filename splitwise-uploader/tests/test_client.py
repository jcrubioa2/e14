"""Unit tests for the Splitwise client. Fully mocked — no network."""

import pytest

from splitwise.client import SplitwiseClient, SplitwiseError, _build_shares


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = str(json_data)

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.calls = []

    def request(self, method, url, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "data": data})
        return self.response


def make_client(response):
    session = FakeSession(response)
    return SplitwiseClient(api_key="test-key", session=session), session


def test_create_expense_equal_builds_request():
    client, session = make_client(FakeResponse({"expenses": [{"id": 1}], "errors": {}}))
    client.create_expense("25.00", "Dinner", group_id=42, split_equally=True)

    call = session.calls[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/create_expense")
    assert session.headers["Authorization"] == "Bearer test-key"
    assert call["data"]["split_equally"] == "true"
    assert call["data"]["cost"] == "25.00"
    assert call["data"]["group_id"] == 42
    assert call["data"]["description"] == "Dinner"


def test_create_expense_custom_shares_expands_fields():
    client, session = make_client(FakeResponse({"expenses": [], "errors": {}}))
    shares = [
        {"user_id": 1, "paid_share": "25.00", "owed_share": "12.50"},
        {"user_id": 2, "paid_share": "0", "owed_share": "12.50"},
    ]
    client.create_expense("25.00", "Dinner", group_id=0, shares=shares)

    data = session.calls[-1]["data"]
    assert data["users__0__user_id"] == 1
    assert data["users__0__paid_share"] == "25.00"
    assert data["users__1__owed_share"] == "12.50"
    assert "split_equally" not in data


def test_share_sum_mismatch_raises():
    client, _ = make_client(FakeResponse({"errors": {}}))
    bad = [
        {"user_id": 1, "paid_share": "25.00", "owed_share": "10.00"},
        {"user_id": 2, "paid_share": "0", "owed_share": "10.00"},
    ]
    with pytest.raises(SplitwiseError):
        client.create_expense("25.00", "x", shares=bad)


def test_errors_object_raises():
    # Splitwise returns 200 with a non-empty errors object on failure.
    client, _ = make_client(FakeResponse({"errors": {"base": ["Invalid API request"]}}))
    with pytest.raises(SplitwiseError):
        client.get_current_user()


def test_http_error_raises():
    client, _ = make_client(FakeResponse({}, status_code=401))
    with pytest.raises(SplitwiseError):
        client.get_current_user()


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("SPLITWISE_API_KEY", raising=False)
    with pytest.raises(SplitwiseError):
        SplitwiseClient()


def test_build_shares_marks_payer():
    shares = _build_shares("30.00", payer="1", owed_items=["1:10.00", "2:10.00", "3:10.00"])
    by_id = {s["user_id"]: s for s in shares}
    assert by_id[1]["paid_share"] == "30.00"
    assert by_id[2]["paid_share"] == "0"
    assert by_id[1]["owed_share"] == "10.00"


def test_build_shares_requires_payer():
    with pytest.raises(SplitwiseError):
        _build_shares("10.00", payer=None, owed_items=["2:10.00"])
