from pathlib import Path

import pandas as pd

from app.live_candles import LiveTick, MinuteCandleStore, parse_yahoo_message


def test_parse_yahoo_tick() -> None:
    tick, cumulative = parse_yahoo_message(
        {
            "id": "BTC-EUR",
            "price": 50000,
            "time": 1_700_000_000_000,
            "day_volume": 100,
        },
        previous_cumulative_volume=90,
    )
    assert tick is not None
    assert tick.symbol == "BTC-EUR"
    assert tick.price == 50000
    assert tick.volume_delta == 10
    assert cumulative == 100


def test_minute_candle_is_finalized(tmp_path: Path) -> None:
    completed: list[tuple[str, int]] = []

    store = MinuteCandleStore(
        symbols=("TEST",),
        data_dir=tmp_path,
        max_bars=100,
        on_completed_bar=lambda symbol, frame: completed.append((symbol, len(frame))),
    )
    store.frames["TEST"] = pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"]
    )

    store.handle_tick(
        LiveTick(
            symbol="TEST",
            price=100,
            timestamp=pd.Timestamp("2026-01-01 10:00:05"),
            volume_delta=2,
        )
    )
    store.handle_tick(
        LiveTick(
            symbol="TEST",
            price=102,
            timestamp=pd.Timestamp("2026-01-01 10:00:30"),
            volume_delta=3,
        )
    )
    store.handle_tick(
        LiveTick(
            symbol="TEST",
            price=101,
            timestamp=pd.Timestamp("2026-01-01 10:01:01"),
            volume_delta=1,
        )
    )

    frame = store.latest_frame("TEST")
    assert len(frame) == 1
    assert frame.iloc[0]["open"] == 100
    assert frame.iloc[0]["high"] == 102
    assert frame.iloc[0]["low"] == 100
    assert frame.iloc[0]["close"] == 102
    assert frame.iloc[0]["volume"] == 5
    assert completed == [("TEST", 1)]
