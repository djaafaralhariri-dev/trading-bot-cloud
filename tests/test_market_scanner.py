from __future__ import annotations

import numpy as np
import pandas as pd

from app.market_scanner import (
    Candidate,
    MarketScanner,
    ScanRecord,
    _find_quotes,
    _split_download,
    select_diverse,
)


def make_history(size: int = 120) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=size, freq="D")
    close = np.linspace(100, 145, size) + np.sin(np.arange(size) / 4)
    return pd.DataFrame(
        {
            "Open": close - 0.3,
            "High": close + 1.2,
            "Low": close - 1.1,
            "Close": close,
            "Volume": np.linspace(8_000_000, 12_000_000, size),
        },
        index=index,
    )


def test_find_quotes_nested() -> None:
    response = {"finance": {"result": [{"quotes": [{"symbol": "AAPL"}]}]}}
    assert _find_quotes(response) == [{"symbol": "AAPL"}]


def test_split_multi_ticker_download() -> None:
    first = make_history(80)
    second = make_history(80) * 1.1
    raw = pd.concat({"AAA": first, "BBB": second}, axis=1)
    frames = _split_download(raw, ["AAA", "BBB"])
    assert set(frames) == {"AAA", "BBB"}
    assert set(frames["AAA"].columns) == {"open", "high", "low", "close", "volume"}


def test_score_candidate() -> None:
    scanner = MarketScanner(screen_func=lambda *args, **kwargs: {}, download_func=lambda *args, **kwargs: None)
    record = scanner.score_candidate(
        Candidate("TEST", "stock", "unit_test"),
        make_history(),
    )
    assert record is not None
    assert 0 <= record.score <= 100
    assert record.price > 0


def record(symbol: str, asset_type: str, score: float) -> ScanRecord:
    return ScanRecord(
        symbol=symbol,
        asset_type=asset_type,
        source="test",
        score=score,
        signal="hold",
        confidence=0,
        strategy="ensemble",
        price=100,
        return_5_pct=1,
        return_20_pct=2,
        atr_pct=2,
        rsi_14=55,
        avg_dollar_volume=10_000_000,
        volume_ratio=1,
        last_bar="2026-01-01",
        reason="test",
    )


def test_diverse_selection() -> None:
    records = [
        record("AAA", "stock", 99),
        record("BBB", "stock", 98),
        record("SPY", "etf", 90),
        record("BTC-EUR", "crypto", 80),
        record("EURUSD=X", "forex", 70),
    ]
    chosen = select_diverse(records, top_n=5)
    assert {item.asset_type for item in chosen} == {"stock", "etf", "crypto", "forex"}
