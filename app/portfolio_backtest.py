from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math

import numpy as np
import pandas as pd

from app.backtest import BacktestTrade, contributors
from app.config import BASE_DIR, settings
from app.indicators import enrich
from app.market_data import YahooMarketData
from app.risk import RiskManager
from app.strategies import Direction, Signal, StrategyEngine


@dataclass(slots=True)
class PortfolioPosition:
    symbol: str
    side: Direction
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_fee: float
    entry_time: str
    strategy: str


@dataclass(slots=True)
class PortfolioResult:
    starting_cash: float
    final_equity: float
    total_return_pct: float
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: float | None
    max_drawdown_pct: float
    sharpe_ratio: float | None
    total_fees: float
    average_trade: float


class PortfolioBacktester:
    def __init__(
        self,
        *,
        strategy_engine: StrategyEngine,
        risk_manager: RiskManager,
        starting_cash: float,
        fee_rate: float,
        slippage_rate: float,
        max_open_positions: int,
    ) -> None:
        self.strategy_engine = strategy_engine
        self.risk_manager = risk_manager
        self.starting_cash = float(starting_cash)
        self.fee_rate = float(fee_rate)
        self.slippage_rate = float(slippage_rate)
        self.max_open_positions = int(max_open_positions)

    def run(
        self,
        frames: dict[str, pd.DataFrame],
    ) -> tuple[PortfolioResult, list[BacktestTrade], pd.DataFrame]:
        prepared: dict[str, pd.DataFrame] = {}
        positions_by_date: dict[str, dict[pd.Timestamp, int]] = {}

        for symbol, raw in frames.items():
            data = enrich(raw)
            if len(data) < 80:
                continue
            prepared[symbol] = data
            positions_by_date[symbol] = {
                timestamp: index
                for index, timestamp in enumerate(data.index)
            }

        if not prepared:
            raise ValueError("Keine Märkte mit ausreichend Daten.")

        dates = sorted(
            set().union(*(set(data.index) for data in prepared.values()))
        )

        cash = self.starting_cash
        positions: dict[str, PortfolioPosition] = {}
        trades: list[BacktestTrade] = []
        equity_points: list[dict[str, object]] = []
        latest_prices: dict[str, float] = {}

        for date in dates:
            current_rows: dict[str, pd.Series] = {}

            for symbol, data in prepared.items():
                index_position = positions_by_date[symbol].get(date)
                if index_position is None:
                    continue
                row = data.iloc[index_position]
                current_rows[symbol] = row
                latest_prices[symbol] = float(row["close"])

            # Erst Stops und Ziele prüfen.
            for symbol in list(positions):
                row = current_rows.get(symbol)
                if row is None:
                    continue

                position = positions[symbol]
                exit_price: float | None = None
                exit_reason: str | None = None

                if position.side is Direction.LONG:
                    if float(row["low"]) <= position.stop_loss:
                        exit_price = position.stop_loss * (1 - self.slippage_rate)
                        exit_reason = "stop_loss"
                    elif float(row["high"]) >= position.take_profit:
                        exit_price = position.take_profit * (1 - self.slippage_rate)
                        exit_reason = "take_profit"
                else:
                    if float(row["high"]) >= position.stop_loss:
                        exit_price = position.stop_loss * (1 + self.slippage_rate)
                        exit_reason = "stop_loss"
                    elif float(row["low"]) <= position.take_profit:
                        exit_price = position.take_profit * (1 + self.slippage_rate)
                        exit_reason = "take_profit"

                if exit_price is not None and exit_reason is not None:
                    cash, trade = self._close(
                        position=position,
                        cash=cash,
                        exit_price=exit_price,
                        exit_time=str(date),
                        exit_reason=exit_reason,
                    )
                    trades.append(trade)
                    del positions[symbol]

            equity = self._equity(cash, positions, latest_prices)

            # Neue Signale mit gemeinsamem Kapital prüfen.
            candidates: list[tuple[Signal, pd.Series]] = []
            for symbol, row in current_rows.items():
                if symbol in positions:
                    continue
                index_position = positions_by_date[symbol][date]
                if index_position < 60:
                    continue

                history = prepared[symbol].iloc[:index_position]
                signal = self.strategy_engine.analyze(symbol, history)
                if signal.direction is Direction.HOLD:
                    continue
                candidates.append((signal, row))

            candidates.sort(key=lambda item: item[0].confidence, reverse=True)

            for signal, row in candidates:
                if len(positions) >= self.max_open_positions:
                    break
                if signal.symbol in positions:
                    continue

                open_price = float(row["open"])
                adjusted_signal = Signal(
                    symbol=signal.symbol,
                    direction=signal.direction,
                    confidence=signal.confidence,
                    entry_price=open_price,
                    stop_loss=self._translate_level(
                        old_entry=signal.entry_price,
                        new_entry=open_price,
                        old_level=signal.stop_loss,
                    ),
                    take_profit=self._translate_level(
                        old_entry=signal.entry_price,
                        new_entry=open_price,
                        old_level=signal.take_profit,
                    ),
                    strategy=signal.strategy,
                    reason=signal.reason,
                )

                plan = self.risk_manager.plan(
                    adjusted_signal,
                    equity=equity,
                    available_cash=cash,
                )
                if plan is None:
                    continue

                fill_price = (
                    open_price * (1 + self.slippage_rate)
                    if adjusted_signal.direction is Direction.LONG
                    else open_price * (1 - self.slippage_rate)
                )
                notional = fill_price * plan.quantity
                entry_fee = notional * self.fee_rate
                required_cash = notional + entry_fee

                if required_cash > cash:
                    continue

                cash -= required_cash
                positions[signal.symbol] = PortfolioPosition(
                    symbol=signal.symbol,
                    side=signal.direction,
                    quantity=plan.quantity,
                    entry_price=fill_price,
                    stop_loss=float(adjusted_signal.stop_loss),
                    take_profit=float(adjusted_signal.take_profit),
                    entry_fee=entry_fee,
                    entry_time=str(date),
                    strategy=signal.strategy,
                )
                equity = self._equity(cash, positions, latest_prices)

            equity_points.append(
                {
                    "time": date,
                    "equity": self._equity(cash, positions, latest_prices),
                    "cash": cash,
                    "open_positions": len(positions),
                }
            )

        final_date = dates[-1]
        for symbol in list(positions):
            position = positions[symbol]
            market_price = latest_prices.get(symbol, position.entry_price)
            exit_price = (
                market_price * (1 - self.slippage_rate)
                if position.side is Direction.LONG
                else market_price * (1 + self.slippage_rate)
            )
            cash, trade = self._close(
                position=position,
                cash=cash,
                exit_price=exit_price,
                exit_time=str(final_date),
                exit_reason="end_of_data",
            )
            trades.append(trade)
            del positions[symbol]

        equity_points.append(
            {
                "time": final_date,
                "equity": cash,
                "cash": cash,
                "open_positions": 0,
            }
        )
        curve = (
            pd.DataFrame(equity_points)
            .drop_duplicates("time", keep="last")
            .set_index("time")
            .sort_index()
        )
        return self._result(cash, trades, curve), trades, curve

    @staticmethod
    def _translate_level(
        *,
        old_entry: float,
        new_entry: float,
        old_level: float | None,
    ) -> float | None:
        if old_level is None:
            return None
        return new_entry + (old_level - old_entry)

    def _close(
        self,
        *,
        position: PortfolioPosition,
        cash: float,
        exit_price: float,
        exit_time: str,
        exit_reason: str,
    ) -> tuple[float, BacktestTrade]:
        if position.side is Direction.LONG:
            gross_pnl = (exit_price - position.entry_price) * position.quantity
        else:
            gross_pnl = (position.entry_price - exit_price) * position.quantity

        exit_fee = exit_price * position.quantity * self.fee_rate
        net_pnl = gross_pnl - position.entry_fee - exit_fee
        reserved = position.entry_price * position.quantity
        cash += reserved + gross_pnl - exit_fee

        trade = BacktestTrade(
            symbol=position.symbol,
            side=position.side.value,
            entry_time=position.entry_time,
            exit_time=exit_time,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            entry_fee=position.entry_fee,
            exit_fee=exit_fee,
            pnl=net_pnl,
            return_pct=(
                net_pnl / (reserved + position.entry_fee) * 100
                if reserved > 0
                else 0.0
            ),
            strategy=position.strategy,
            exit_reason=exit_reason,
        )
        return cash, trade

    @staticmethod
    def _equity(
        cash: float,
        positions: dict[str, PortfolioPosition],
        latest_prices: dict[str, float],
    ) -> float:
        equity = cash
        for symbol, position in positions.items():
            price = latest_prices.get(symbol, position.entry_price)
            reserved = position.entry_price * position.quantity
            pnl = (
                (price - position.entry_price) * position.quantity
                if position.side is Direction.LONG
                else (position.entry_price - price) * position.quantity
            )
            equity += reserved + pnl
        return equity

    def _result(
        self,
        final_equity: float,
        trades: list[BacktestTrade],
        curve: pd.DataFrame,
    ) -> PortfolioResult:
        pnls = [trade.pnl for trade in trades]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_loss = abs(sum(losses))
        profit_factor = (
            sum(wins) / gross_loss
            if gross_loss > 0
            else (float("inf") if wins else None)
        )

        equity = curve["equity"].astype(float)
        running_peak = equity.cummax()
        drawdown = (equity / running_peak - 1) * 100
        returns = equity.pct_change().dropna()
        sharpe: float | None = None
        if len(returns) > 1 and float(returns.std()) > 0:
            sharpe = float(np.sqrt(252) * returns.mean() / returns.std())

        return PortfolioResult(
            starting_cash=self.starting_cash,
            final_equity=final_equity,
            total_return_pct=(final_equity / self.starting_cash - 1) * 100,
            trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate_pct=len(wins) / len(trades) * 100 if trades else 0.0,
            profit_factor=profit_factor,
            max_drawdown_pct=abs(float(drawdown.min())) if not drawdown.empty else 0.0,
            sharpe_ratio=sharpe,
            total_fees=sum(t.entry_fee + t.exit_fee for t in trades),
            average_trade=float(np.mean(pnls)) if pnls else 0.0,
        )


def format_metric(value: float | None) -> str:
    if value is None:
        return "-"
    if math.isinf(value):
        return "∞"
    return f"{value:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemeinsamer Portfolio-Backtest")
    parser.add_argument("--period", default="5y")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--symbols", default=",".join(settings.symbols))
    args = parser.parse_args()

    symbols = tuple(
        item.strip().upper()
        for item in args.symbols.split(",")
        if item.strip()
    )
    source = YahooMarketData()
    frames = source.fetch_many(
        symbols,
        period=args.period,
        interval=args.interval,
    )

    tester = PortfolioBacktester(
        strategy_engine=StrategyEngine(
            settings.min_signal_confidence,
            settings.min_strategy_agreement,
        ),
        risk_manager=RiskManager(
            settings.risk_per_trade,
            settings.max_position_fraction,
        ),
        starting_cash=settings.starting_cash,
        fee_rate=settings.fee_rate,
        slippage_rate=settings.slippage_rate,
        max_open_positions=settings.max_open_positions,
    )
    result, trades, curve = tester.run(frames)

    print("\n" + "=" * 64)
    print("GEMEINSAMER MULTI-ASSET-PORTFOLIO-BACKTEST")
    print(f"Startkapital:       {result.starting_cash:10.2f} €")
    print(f"Endkapital:         {result.final_equity:10.2f} €")
    print(f"Rendite:            {result.total_return_pct:10.2f} %")
    print(f"Trades:             {result.trades:10d}")
    print(f"Trefferquote:       {result.win_rate_pct:10.2f} %")
    print(f"Profit Factor:      {format_metric(result.profit_factor):>10}")
    print(f"Max. Drawdown:      {result.max_drawdown_pct:10.2f} %")
    print(f"Sharpe Ratio:       {format_metric(result.sharpe_ratio):>10}")
    print(f"Gebühren:           {result.total_fees:10.2f} €")
    print(f"Ø Trade netto:      {result.average_trade:10.2f} €")
    print("=" * 64)

    output = BASE_DIR / "portfolio_backtest_output"
    output.mkdir(exist_ok=True)
    pd.DataFrame([asdict(result)]).to_csv(output / "summary.csv", index=False)
    pd.DataFrame([asdict(trade) for trade in trades]).to_csv(
        output / "trades.csv",
        index=False,
    )
    curve.to_csv(output / "equity_curve.csv")
    (output / "summary.json").write_text(
        json.dumps(asdict(result), indent=2),
        encoding="utf-8",
    )
    print(f"\nErgebnisse gespeichert in: {output}")


if __name__ == "__main__":
    main()
