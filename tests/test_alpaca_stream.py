from __future__ import annotations

import json

import pandas as pd

from app.alpaca_stream import (
    AlpacaStockStream,
    from_alpaca_symbol,
    parse_bar,
    parse_trade,
    to_alpaca_symbol,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def close(self) -> None:
        self.closed = True


def test_symbol_mapping() -> None:
    assert to_alpaca_symbol("BRK-B") == "BRK.B"
    assert from_alpaca_symbol("BRK.B") == "BRK-B"
    assert to_alpaca_symbol("AAPL") == "AAPL"


def test_parse_trade() -> None:
    tick = parse_trade(
        {
            "T": "t",
            "S": "AAPL",
            "p": 123.45,
            "s": 7,
            "t": "2026-08-06T12:34:56.123Z",
            "x": "V",
        }
    )
    assert tick is not None
    assert tick.symbol == "AAPL"
    assert tick.price == 123.45
    assert tick.timestamp == pd.Timestamp("2026-08-06 12:34:56.123")


def test_parse_bar() -> None:
    bar = parse_bar(
        {
            "T": "b",
            "S": "SPY",
            "o": 600,
            "h": 601,
            "l": 599,
            "c": 600.5,
            "v": 500,
            "t": "2026-08-06T12:35:00Z",
        }
    )
    assert bar is not None
    assert bar.symbol == "SPY"
    assert bar.close == 600.5
    assert bar.timestamp == pd.Timestamp("2026-08-06 12:35:00")


def test_authentication_and_subscription_messages() -> None:
    ready: list[bool] = []
    stream = AlpacaStockStream(
        api_key="paper-key",
        secret_key="paper-secret",
        symbols=("AAPL", "BRK-B"),
        feed="iex",
        on_trade=lambda _tick: None,
        on_bar=lambda _bar: None,
        on_ready=lambda: ready.append(True),
    )
    ws = FakeWebSocket()
    stream._on_open(ws)  # type: ignore[arg-type]
    assert ws.sent[0] == {
        "action": "auth",
        "key": "paper-key",
        "secret": "paper-secret",
    }

    stream._on_message(
        ws,  # type: ignore[arg-type]
        json.dumps([{"T": "success", "msg": "authenticated"}]),
    )
    assert ws.sent[1] == {
        "action": "subscribe",
        "trades": ["AAPL", "BRK.B"],
        "bars": ["AAPL", "BRK.B"],
    }

    stream._on_message(
        ws,  # type: ignore[arg-type]
        json.dumps([{"T": "subscription", "trades": ["AAPL"], "bars": ["AAPL"]}]),
    )
    assert ready == [True]
