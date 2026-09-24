"""
agent/tests/test_api_client.py — every `<behavior>` bullet for `agent/api_client.py`
(39-14-PLAN.md Task 1). `requests` is stubbed throughout — no network is ever touched.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from agent import api_client


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        json_body: Any = None,
        raw_text: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self._raw_text = raw_text
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300

    def json(self) -> Any:
        if self._raw_text is not None:
            # Mirrors requests' own behaviour: json() raises on non-JSON text.
            return json.loads(self._raw_text)
        return self._json_body


class _FakeSession:
    """Records every call made through `requests.request` and returns canned responses."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)


@pytest.fixture()
def fake_requests(monkeypatch: pytest.MonkeyPatch):
    def _install(responses: list[_FakeResponse]) -> _FakeSession:
        session = _FakeSession(responses)
        monkeypatch.setattr(api_client.requests, "request", session)
        return session

    return _install


# ---------------------------------------------------------------------------
# Header / protocol behaviour
# ---------------------------------------------------------------------------

def test_fetch_accounts_sends_bearer_token_and_protocol_header(fake_requests) -> None:
    session = fake_requests(
        [_FakeResponse(200, json_body={"token": "tok_new", "accounts": []})]
    )
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    client.fetch_accounts()

    call = session.calls[0]
    assert call["headers"]["Authorization"] == "Bearer tok_old"
    assert call["headers"]["X-Agent-Protocol"] == str(api_client.AGENT_PROTOCOL_VERSION)


def test_redeem_pairing_code_sends_no_authorization_header(fake_requests) -> None:
    session = fake_requests([_FakeResponse(200, json_body={"token": "tok_new"})])
    client = api_client.ApiClient("https://treedger.com")

    client.redeem_pairing_code("ABCD1234")

    call = session.calls[0]
    assert "Authorization" not in call["headers"]
    assert call["headers"]["X-Agent-Protocol"] == str(api_client.AGENT_PROTOCOL_VERSION)


def test_redeem_pairing_code_returns_the_token(fake_requests) -> None:
    fake_requests([_FakeResponse(200, json_body={"token": "tok_new"})])
    client = api_client.ApiClient("https://treedger.com")

    assert client.redeem_pairing_code("ABCD1234") == "tok_new"


# ---------------------------------------------------------------------------
# Status-code -> exception mapping
# ---------------------------------------------------------------------------

def test_fetch_accounts_on_401_raises_unauthorized(fake_requests) -> None:
    fake_requests([_FakeResponse(401, json_body={"error": "rejected"})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    with pytest.raises(api_client.AgentUnauthorizedError):
        client.fetch_accounts()


def test_fetch_accounts_on_401_is_not_a_plain_agent_api_error_of_another_kind(fake_requests) -> None:
    fake_requests([_FakeResponse(401, json_body={"error": "rejected"})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    with pytest.raises(api_client.AgentApiError) as exc_info:
        client.fetch_accounts()
    assert isinstance(exc_info.value, api_client.AgentUnauthorizedError)
    assert not isinstance(exc_info.value, api_client.AgentRateLimitedError)


def test_fetch_accounts_on_429_raises_rate_limited_with_retry_hint(fake_requests) -> None:
    fake_requests(
        [_FakeResponse(429, json_body={"error": "rate_limited"}, headers={"Retry-After": "42"})]
    )
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    with pytest.raises(api_client.AgentRateLimitedError) as exc_info:
        client.fetch_accounts()
    assert exc_info.value.retry_after_seconds == 42


def test_fetch_accounts_on_429_without_retry_after_header_still_raises(fake_requests) -> None:
    fake_requests([_FakeResponse(429, json_body={"error": "rate_limited"})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    with pytest.raises(api_client.AgentRateLimitedError) as exc_info:
        client.fetch_accounts()
    assert exc_info.value.retry_after_seconds is None


def test_protocol_refusal_raises_agent_protocol_error(fake_requests) -> None:
    fake_requests([_FakeResponse(400, json_body={"error": "protocol"})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    with pytest.raises(api_client.AgentProtocolError):
        client.fetch_accounts()


def test_other_error_status_raises_plain_agent_api_error(fake_requests) -> None:
    fake_requests([_FakeResponse(500, json_body={"error": "internal"})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    with pytest.raises(api_client.AgentApiError) as exc_info:
        client.fetch_accounts()
    assert not isinstance(exc_info.value, api_client.AgentUnauthorizedError)
    assert not isinstance(exc_info.value, api_client.AgentRateLimitedError)
    assert not isinstance(exc_info.value, api_client.AgentProtocolError)


def test_non_json_response_raises_a_clear_error(fake_requests) -> None:
    fake_requests([_FakeResponse(200, raw_text="<html>not json</html>")])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    with pytest.raises(api_client.AgentApiError):
        client.fetch_accounts()


# ---------------------------------------------------------------------------
# post_trades chunking
# ---------------------------------------------------------------------------

def _trade(position_id: int) -> dict:
    return {
        "mt5PositionId": position_id,
        "pair": "EURUSD",
        "direction": "BUY",
        "openedAt": "2024-01-01T00:00:00Z",
        "closedAt": "2024-01-01T01:00:00Z",
        "entryPrice": 1.1,
        "exitPrice": 1.2,
        "volume": 1.0,
        "profit": 10.0,
        "commission": -1.0,
        "swap": 0.0,
        "session": "London",
        "initialStopLoss": 1.05,
        "takeProfit": 1.25,
    }


def test_post_trades_at_exactly_the_batch_limit_sends_one_batch(fake_requests) -> None:
    trades = [_trade(i) for i in range(api_client.MAX_TRADES_PER_BATCH)]
    session = fake_requests([_FakeResponse(200, json_body={"inserted": 500, "updated": 0, "skipped": 0})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    client.post_trades(account_id="acc-1", deposit_base=1000.0, currency="USD", is_demo=False, trades=trades)

    assert len(session.calls) == 1
    body = session.calls[0]["json"]
    assert body["batchIndex"] == 0
    assert body["isFinalBatch"] is True
    assert len(body["trades"]) == api_client.MAX_TRADES_PER_BATCH


def test_post_trades_one_over_the_batch_limit_sends_two_batches(fake_requests) -> None:
    trades = [_trade(i) for i in range(api_client.MAX_TRADES_PER_BATCH + 1)]
    session = fake_requests(
        [
            _FakeResponse(200, json_body={"inserted": 500, "updated": 0, "skipped": 0}),
            _FakeResponse(200, json_body={"inserted": 1, "updated": 0, "skipped": 0}),
        ]
    )
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    result = client.post_trades(
        account_id="acc-1", deposit_base=1000.0, currency="USD", is_demo=False, trades=trades
    )

    assert len(session.calls) == 2
    first_body = session.calls[0]["json"]
    second_body = session.calls[1]["json"]
    assert first_body["batchIndex"] == 0
    assert first_body["isFinalBatch"] is False
    assert len(first_body["trades"]) == api_client.MAX_TRADES_PER_BATCH
    assert second_body["batchIndex"] == 1
    assert second_body["isFinalBatch"] is True
    assert len(second_body["trades"]) == 1
    assert result == {"inserted": 501, "updated": 0, "skipped": 0}


def test_post_trades_one_under_the_batch_limit_sends_one_batch(fake_requests) -> None:
    trades = [_trade(i) for i in range(api_client.MAX_TRADES_PER_BATCH - 1)]
    session = fake_requests([_FakeResponse(200, json_body={"inserted": 499, "updated": 0, "skipped": 0})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    client.post_trades(account_id="acc-1", deposit_base=1000.0, currency="USD", is_demo=False, trades=trades)

    assert len(session.calls) == 1
    body = session.calls[0]["json"]
    assert body["isFinalBatch"] is True
    assert len(body["trades"]) == api_client.MAX_TRADES_PER_BATCH - 1


def test_post_trades_with_empty_list_still_sends_one_final_batch(fake_requests) -> None:
    session = fake_requests([_FakeResponse(200, json_body={"inserted": 0, "updated": 0, "skipped": 0})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    client.post_trades(account_id="acc-1", deposit_base=1000.0, currency="USD", is_demo=False, trades=[])

    assert len(session.calls) == 1
    body = session.calls[0]["json"]
    assert body["batchIndex"] == 0
    assert body["isFinalBatch"] is True
    assert body["trades"] == []


# ---------------------------------------------------------------------------
# post_account_status
# ---------------------------------------------------------------------------

def test_post_account_status_sends_null_stats_on_failure(fake_requests) -> None:
    session = fake_requests([_FakeResponse(200, json_body={"ok": True})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    client.post_account_status(account_id="acc-1", outcome="auth_failed", is_demo=None)

    body = session.calls[0]["json"]
    assert body["stats"] is None
    assert body["equityPoints"] is None
    assert body["outcome"] == "auth_failed"


def test_post_account_status_sends_stats_on_ok(fake_requests) -> None:
    session = fake_requests([_FakeResponse(200, json_body={"ok": True})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")
    stats = {"returnAll": 1.0}
    equity_points = [{"ts": "2024-01-01T00:00:00Z", "equityPct": 1.0}]

    client.post_account_status(
        account_id="acc-1", outcome="ok", is_demo=False, stats=stats, equity_points=equity_points
    )

    body = session.calls[0]["json"]
    assert body["stats"] == stats
    assert body["equityPoints"] == equity_points


# ---------------------------------------------------------------------------
# TLS / no-verify-argument discipline (mirrors the module-level grep gate)
# ---------------------------------------------------------------------------

def test_no_call_ever_passes_a_verify_argument(fake_requests) -> None:
    session = fake_requests([_FakeResponse(200, json_body={"token": "tok_new", "accounts": []})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    client.fetch_accounts()

    assert "verify" not in session.calls[0]


def test_every_request_carries_a_connect_and_read_timeout(fake_requests) -> None:
    session = fake_requests([_FakeResponse(200, json_body={"token": "tok_new", "accounts": []})])
    client = api_client.ApiClient("https://treedger.com", token="tok_old")

    client.fetch_accounts()

    assert "timeout" in session.calls[0]
    assert isinstance(session.calls[0]["timeout"], tuple)
