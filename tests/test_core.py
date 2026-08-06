from pathlib import Path

import numpy as np
import pandas as pd

from app.indicators import enrich
from app.paper_broker import PaperBroker
from app.risk import RiskManager
from app.strategies import Direction, Signal


def test_indicators() -> None:
    size = 120
    close = np.linspace(100, 140, size)
    frame = pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.linspace(1_000, 2_000, size),
        }
    )
    result = enrich(frame)
    assert not result.empty
    assert {"sma_20", "sma_50", "rsi_14", "atr_14"}.issubset(result.columns)


def test_risk_limit() -> None:
    manager = RiskManager(0.005, 0.35)
    signal = Signal(
        symbol="TEST",
        direction=Direction.LONG,
        confidence=0.8,
        entry_price=100,
        stop_loss=95,
        take_profit=110,
        strategy="test",
        reason="test",
    )
    plan = manager.plan(signal, equity=250, available_cash=250)
    assert plan is not None
    assert plan.notional <= 87.5 + 1e-9


def test_paper_long_profit(tmp_path: Path) -> None:
    broker = PaperBroker(
        starting_cash=250,
        fee_rate=0,
        slippage_rate=0,
        state_path=tmp_path / "state.json",
    )
    signal = Signal(
        symbol="TEST",
        direction=Direction.LONG,
        confidence=0.8,
        entry_price=100,
        stop_loss=95,
        take_profit=110,
        strategy="test",
        reason="test",
    )
    broker.open_position(signal, 1)
    broker.update_positions({"TEST": 110})
    assert broker.cash == 260
    assert not broker.positions
