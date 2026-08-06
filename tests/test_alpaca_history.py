from __future__ import annotations

from typing import Any

from app.alpaca_history import AlpacaHistoricalBars, DATA_BASE_URL


class FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {
            "bars": {
                "AAPL": [
                    {"t": "2026-08-06T13:30:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 500},
                    {"t": "2026-08-06T13:31:00Z", "o": 100.5, "h": 102, "l": 100, "c": 101.5, "v": 600},
                ]
            },
            "next_page_token": None,
        }


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, *, params: dict[str, Any], timeout: int) -> FakeResponse:
        assert url == f"{DATA_BASE_URL}/v2/stocks/bars"
        assert timeout == 30
        self.calls.append((url, dict(params)))
        return FakeResponse()


def test_parses_alpaca_minute_bars() -> None:
    session = FakeSession()
    client = AlpacaHistoricalBars(
        api_key="paper-key",
        secret_key="paper-secret",
        feed="iex",
        session=session,
    )
    result = client.fetch_recent(("AAPL",), max_bars_per_symbol=600)
    frame = result["AAPL"]
    assert len(frame) == 2
    assert frame.iloc[-1]["close"] == 101.5
    assert session.calls[0][1]["feed"] == "iex"
