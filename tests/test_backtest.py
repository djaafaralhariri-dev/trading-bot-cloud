import numpy as np
import pandas as pd

from app.backtest import Backtester
from app.risk import RiskManager
from app.strategies import Direction, Signal


class AlwaysLongEngine:
    def analyze(self, symbol: str, data: pd.DataFrame) -> Signal:
        price = float(data.iloc[-1]["close"])
        return Signal(
            symbol=symbol,
            direction=Direction.LONG,
            confidence=0.8,
            entry_price=price,
            stop_loss=price * 0.98,
            take_profit=price * 1.04,
            strategy="test_long",
            reason="synthetic test",
        )


def synthetic_uptrend(size: int = 220) -> pd.DataFrame:
    close = np.linspace(100, 150, size)
    index = pd.date_range("2024-01-01", periods=size, freq="D")
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.015,
            "low": close * 0.995,
            "close": close,
            "volume": np.linspace(1_000, 2_000, size),
        },
        index=index,
    )


def test_backtester_runs_and_creates_metrics() -> None:
    backtester = Backtester(
        strategy_engine=AlwaysLongEngine(),
        risk_manager=RiskManager(0.005, 0.35),
        starting_cash=250,
        fee_rate=0,
        slippage_rate=0,
    )
    result, trades, equity = backtester.run("TEST", synthetic_uptrend())

    assert result.trades > 0
    assert result.final_equity > 0
    assert not equity.empty
    assert len(trades) == result.trades
