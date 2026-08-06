from __future__ import annotations

from pathlib import Path
from typing import Any

from app.alpaca_broker import AlpacaPaperBroker, PAPER_BASE_URL
from app.strategies import Direction, Signal


class FakeResponse:
    def __init__(self, data: Any, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = {"X-Request-ID": "test-request"}
        self.content = b"x"
        self.text = str(data)

    def json(self) -> Any:
        return self._data


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.position_open = False

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: int = 20,
    ) -> FakeResponse:
        del params, timeout
        self.calls.append((method, url, json))
        assert url.startswith(PAPER_BASE_URL)
        path = url.removeprefix(PAPER_BASE_URL)
        if method == "GET" and path == "/v2/account":
            return FakeResponse(
                {
                    "account_number": "PA12345678",
                    "status": "ACTIVE",
                    "currency": "USD",
                    "cash": "250",
                    "equity": "250",
                    "last_equity": "250",
                    "buying_power": "250",
                }
            )
        if method == "GET" and path == "/v2/positions":
            if not self.position_open:
                return FakeResponse([])
            return FakeResponse(
                [
                    {
                        "symbol": "AAPL",
                        "side": "long",
                        "qty": "0.5",
                        "avg_entry_price": "100",
                        "market_value": "50",
                    }
                ]
            )
        if method == "GET" and path == "/v2/orders":
            return FakeResponse([])
        if method == "GET" and path == "/v2/assets/AAPL":
            return FakeResponse(
                {
                    "symbol": "AAPL",
                    "status": "active",
                    "tradable": True,
                    "fractionable": True,
                    "shortable": True,
                    "class": "us_equity",
                }
            )
        if method == "GET" and path.startswith("/v2/assets/"):
            return FakeResponse({"message": "asset not found"}, 404)
        if method == "POST" and path == "/v2/orders":
            self.position_open = True
            return FakeResponse({"id": "order-1", "status": "accepted"})
        if method == "DELETE" and path == "/v2/positions/AAPL":
            self.position_open = False
            return FakeResponse({"id": "close-1", "status": "accepted"})
        raise AssertionError(f"Unexpected request: {method} {path}")


def make_broker(tmp_path: Path, session: FakeSession, *, enabled: bool = True) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        api_key="paper-key",
        secret_key="paper-secret",
        state_path=tmp_path / "alpaca_state.json",
        order_execution_enabled=enabled,
        allow_shorts=False,
        max_order_notional=75,
        refresh_seconds=2,
        require_fractionable=True,
        session=session,
    )


def test_filters_to_tradable_us_fractional_assets(tmp_path: Path) -> None:
    broker = make_broker(tmp_path, FakeSession())
    assert broker.filter_tradable_symbols(("AAPL", "SAP.DE", "EURUSD=X")) == ("AAPL",)


def test_submits_only_to_hardcoded_paper_endpoint(tmp_path: Path) -> None:
    session = FakeSession()
    broker = make_broker(tmp_path, session)
    signal = Signal(
        symbol="AAPL",
        direction=Direction.LONG,
        confidence=0.8,
        entry_price=100,
        stop_loss=95,
        take_profit=110,
        strategy="test",
        reason="test",
    )
    position = broker.open_position(signal, quantity=0.5)
    assert position is not None
    post_calls = [call for call in session.calls if call[0] == "POST"]
    assert post_calls
    method, url, payload = post_calls[-1]
    assert method == "POST"
    assert url == f"{PAPER_BASE_URL}/v2/orders"
    assert payload is not None
    assert payload["notional"] == "50.00"
    assert "api.alpaca.markets" not in url.replace("paper-api.alpaca.markets", "")


def test_orders_are_blocked_by_default_switch(tmp_path: Path) -> None:
    session = FakeSession()
    broker = make_broker(tmp_path, session, enabled=False)
    signal = Signal(
        symbol="AAPL",
        direction=Direction.LONG,
        confidence=0.8,
        entry_price=100,
        stop_loss=95,
        take_profit=110,
        strategy="test",
        reason="test",
    )
    assert broker.open_position(signal, quantity=0.5) is None
    assert not [call for call in session.calls if call[0] == "POST"]


def test_short_is_blocked(tmp_path: Path) -> None:
    session = FakeSession()
    broker = make_broker(tmp_path, session)
    signal = Signal(
        symbol="AAPL",
        direction=Direction.SHORT,
        confidence=0.8,
        entry_price=100,
        stop_loss=105,
        take_profit=90,
        strategy="test",
        reason="test",
    )
    assert broker.open_position(signal, quantity=0.5) is None
    assert not [call for call in session.calls if call[0] == "POST"]


def test_submits_whole_share_bracket_order_for_github_cloud(tmp_path: Path) -> None:
    session = FakeSession()
    broker = make_broker(tmp_path, session)
    signal = Signal(
        symbol="AAPL",
        direction=Direction.LONG,
        confidence=0.8,
        entry_price=50,
        stop_loss=48,
        take_profit=55,
        strategy="ensemble[test]",
        reason="test",
    )
    position = broker.open_bracket_position(
        signal,
        quantity=1.7,
        client_order_id="gh10-aapl-test",
    )
    assert position is not None
    post_calls = [call for call in session.calls if call[0] == "POST"]
    payload = post_calls[-1][2]
    assert payload is not None
    assert payload["qty"] == "1"
    assert payload["order_class"] == "bracket"
    assert payload["take_profit"] == {"limit_price": "55.00"}
    assert payload["stop_loss"] == {"stop_price": "48.00"}
    assert payload["client_order_id"] == "gh10-aapl-test"
