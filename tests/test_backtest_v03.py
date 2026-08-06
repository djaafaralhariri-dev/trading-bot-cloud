import numpy as np
import pandas as pd

from app.backtest import Backtester, breakdown_frame
from app.portfolio_backtest import PortfolioBacktester
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
            strategy="ensemble[trend, momentum]",
            reason="synthetic",
        )


def synthetic_frame(size: int = 240, start: float = 100, end: float = 150) -> pd.DataFrame:
    close = np.linspace(start, end, size)
    index = pd.date_range("2024-01-01", periods=size, freq="D")
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.02,
            "low": close * 0.995,
            "close": close,
            "volume": np.linspace(1_000, 2_000, size),
        },
        index=index,
    )


def test_trade_pnl_includes_both_fees() -> None:
    tester = Backtester(
        strategy_engine=AlwaysLongEngine(),
        risk_manager=RiskManager(0.005, 0.35),
        starting_cash=250,
        fee_rate=0.01,
        slippage_rate=0,
    )
    result, trades, _ = tester.run("TEST", synthetic_frame())
    assert trades
    assert result.total_fees > 0
    assert all(trade.entry_fee > 0 and trade.exit_fee > 0 for trade in trades)
    assert abs(sum(trade.pnl for trade in trades) - (result.final_equity - 250)) < 1e-6


def test_breakdown_contains_contributing_strategies() -> None:
    tester = Backtester(
        strategy_engine=AlwaysLongEngine(),
        risk_manager=RiskManager(0.005, 0.35),
        starting_cash=250,
        fee_rate=0,
        slippage_rate=0,
    )
    _, trades, _ = tester.run("TEST", synthetic_frame())
    breakdown = breakdown_frame(trades, mode="strategy")
    assert {"trend", "momentum"}.issubset(set(breakdown["strategy"]))


def test_shared_portfolio_uses_one_starting_balance() -> None:
    tester = PortfolioBacktester(
        strategy_engine=AlwaysLongEngine(),
        risk_manager=RiskManager(0.005, 0.35),
        starting_cash=250,
        fee_rate=0,
        slippage_rate=0,
        max_open_positions=2,
    )
    result, trades, curve = tester.run(
        {
            "AAA": synthetic_frame(),
            "BBB": synthetic_frame(start=80, end=120),
        }
    )
    assert result.starting_cash == 250
    assert result.final_equity > 0
    assert not curve.empty
    assert trades
