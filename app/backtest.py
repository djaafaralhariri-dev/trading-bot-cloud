from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re

import numpy as np
import pandas as pd

from app.config import BASE_DIR, settings
from app.indicators import enrich
from app.market_data import YahooMarketData
from app.risk import RiskManager
from app.strategies import Direction, Signal, StrategyEngine


@dataclass(slots=True)
class BacktestPosition:
    side: Direction
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_fee: float
    entry_time: str
    strategy: str


@dataclass(slots=True)
class BacktestTrade:
    symbol: str
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: float
    entry_fee: float
    exit_fee: float
    pnl: float
    return_pct: float
    strategy: str
    exit_reason: str


@dataclass(slots=True)
class BacktestResult:
    symbol: str
    starting_cash: float
    final_equity: float
    total_return_pct: float
    buy_hold_return_pct: float
    alpha_vs_buy_hold_pct: float
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: float | None
    max_drawdown_pct: float
    sharpe_ratio: float | None
    average_trade: float
    best_trade: float
    worst_trade: float
    total_fees: float


@dataclass(slots=True)
class WalkForwardFold:
    symbol: str
    fold: int
    start: str
    end: str
    return_pct: float
    buy_hold_return_pct: float
    trades: int
    profit_factor: float | None
    max_drawdown_pct: float


class Backtester:
    def __init__(
        self,
        *,
        strategy_engine: StrategyEngine,
        risk_manager: RiskManager,
        starting_cash: float,
        fee_rate: float,
        slippage_rate: float,
    ) -> None:
        self.strategy_engine = strategy_engine
        self.risk_manager = risk_manager
        self.starting_cash = float(starting_cash)
        self.fee_rate = float(fee_rate)
        self.slippage_rate = float(slippage_rate)

    def run(
        self,
        symbol: str,
        raw_data: pd.DataFrame,
        *,
        trade_start: pd.Timestamp | None = None,
    ) -> tuple[BacktestResult, list[BacktestTrade], pd.DataFrame]:
        data = enrich(raw_data)
        if len(data) < 80:
            raise ValueError(f"{symbol}: zu wenige Daten für einen Backtest.")

        if trade_start is not None:
            trade_start = pd.Timestamp(trade_start)
            eligible = data.index[data.index >= trade_start]
            if len(eligible) < 2:
                raise ValueError(f"{symbol}: Testfenster enthält zu wenige Daten.")
            first_trade_index = eligible[0]
        else:
            first_trade_index = data.index[60]

        cash = self.starting_cash
        position: BacktestPosition | None = None
        trades: list[BacktestTrade] = []
        equity_points: list[dict[str, object]] = []

        for i in range(60, len(data)):
            row = data.iloc[i]
            now = data.index[i]
            open_price = float(row["open"])
            high_price = float(row["high"])
            low_price = float(row["low"])
            close_price = float(row["close"])

            if now < first_trade_index:
                continue

            if position is not None:
                exit_price: float | None = None
                exit_reason: str | None = None

                if position.side is Direction.LONG:
                    if low_price <= position.stop_loss:
                        exit_price = position.stop_loss * (1 - self.slippage_rate)
                        exit_reason = "stop_loss"
                    elif high_price >= position.take_profit:
                        exit_price = position.take_profit * (1 - self.slippage_rate)
                        exit_reason = "take_profit"
                else:
                    if high_price >= position.stop_loss:
                        exit_price = position.stop_loss * (1 + self.slippage_rate)
                        exit_reason = "stop_loss"
                    elif low_price <= position.take_profit:
                        exit_price = position.take_profit * (1 + self.slippage_rate)
                        exit_reason = "take_profit"

                if exit_price is not None and exit_reason is not None:
                    cash, trade = self._close_position(
                        symbol=symbol,
                        position=position,
                        cash=cash,
                        exit_price=exit_price,
                        exit_time=str(now),
                        exit_reason=exit_reason,
                    )
                    trades.append(trade)
                    position = None

            history = data.iloc[:i]
            if position is None and len(history) >= 60:
                signal = self.strategy_engine.analyze(symbol, history)

                if signal.direction is not Direction.HOLD:
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

                    equity_before = self._equity(cash, position, close_price)
                    plan = self.risk_manager.plan(
                        adjusted_signal,
                        equity=equity_before,
                        available_cash=cash,
                    )
                    if plan is not None:
                        fill_price = (
                            open_price * (1 + self.slippage_rate)
                            if adjusted_signal.direction is Direction.LONG
                            else open_price * (1 - self.slippage_rate)
                        )
                        notional = fill_price * plan.quantity
                        entry_fee = notional * self.fee_rate
                        required_cash = notional + entry_fee

                        if required_cash <= cash:
                            cash -= required_cash
                            position = BacktestPosition(
                                side=adjusted_signal.direction,
                                quantity=plan.quantity,
                                entry_price=fill_price,
                                stop_loss=float(adjusted_signal.stop_loss),
                                take_profit=float(adjusted_signal.take_profit),
                                entry_fee=entry_fee,
                                entry_time=str(now),
                                strategy=adjusted_signal.strategy,
                            )

            equity_points.append(
                {
                    "time": now,
                    "equity": self._equity(cash, position, close_price),
                }
            )

        if position is not None:
            last_time = str(data.index[-1])
            last_price = float(data.iloc[-1]["close"])
            exit_price = (
                last_price * (1 - self.slippage_rate)
                if position.side is Direction.LONG
                else last_price * (1 + self.slippage_rate)
            )
            cash, trade = self._close_position(
                symbol=symbol,
                position=position,
                cash=cash,
                exit_price=exit_price,
                exit_time=last_time,
                exit_reason="end_of_data",
            )
            trades.append(trade)
            equity_points.append({"time": data.index[-1], "equity": cash})

        if not equity_points:
            raise ValueError(f"{symbol}: keine auswertbaren Punkte.")

        equity_curve = (
            pd.DataFrame(equity_points)
            .drop_duplicates("time", keep="last")
            .set_index("time")
            .sort_index()
        )

        buy_hold_return = self._buy_hold_return(
            data=data,
            first_trade_index=first_trade_index,
        )
        result = self._calculate_result(
            symbol=symbol,
            final_equity=cash,
            trades=trades,
            equity_curve=equity_curve,
            buy_hold_return_pct=buy_hold_return,
        )
        return result, trades, equity_curve

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

    def _buy_hold_return(
        self,
        *,
        data: pd.DataFrame,
        first_trade_index: pd.Timestamp,
    ) -> float:
        eligible = data.loc[data.index >= first_trade_index]
        if len(eligible) < 2:
            return 0.0

        entry_market = float(eligible.iloc[0]["open"])
        exit_market = float(eligible.iloc[-1]["close"])
        entry_fill = entry_market * (1 + self.slippage_rate)
        exit_fill = exit_market * (1 - self.slippage_rate)

        quantity = self.starting_cash / (entry_fill * (1 + self.fee_rate))
        final_cash = quantity * exit_fill * (1 - self.fee_rate)
        return (final_cash / self.starting_cash - 1) * 100

    def _close_position(
        self,
        *,
        symbol: str,
        position: BacktestPosition,
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
        reserved_cash = position.entry_price * position.quantity

        # Die Einstiegsgebühr wurde beim Öffnen bereits vom Cash abgezogen.
        cash += reserved_cash + gross_pnl - exit_fee

        trade_return = (
            net_pnl / (reserved_cash + position.entry_fee) * 100
            if reserved_cash > 0
            else 0.0
        )
        trade = BacktestTrade(
            symbol=symbol,
            side=position.side.value,
            entry_time=position.entry_time,
            exit_time=exit_time,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            entry_fee=position.entry_fee,
            exit_fee=exit_fee,
            pnl=net_pnl,
            return_pct=trade_return,
            strategy=position.strategy,
            exit_reason=exit_reason,
        )
        return cash, trade

    @staticmethod
    def _equity(
        cash: float,
        position: BacktestPosition | None,
        current_price: float,
    ) -> float:
        if position is None:
            return cash

        reserved = position.entry_price * position.quantity
        if position.side is Direction.LONG:
            pnl = (current_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - current_price) * position.quantity
        return cash + reserved + pnl

    def _calculate_result(
        self,
        *,
        symbol: str,
        final_equity: float,
        trades: list[BacktestTrade],
        equity_curve: pd.DataFrame,
        buy_hold_return_pct: float,
    ) -> BacktestResult:
        pnls = [trade.pnl for trade in trades]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else (float("inf") if gross_profit > 0 else None)
        )

        equity = equity_curve["equity"].astype(float)
        running_peak = equity.cummax()
        drawdown = (equity / running_peak - 1) * 100
        max_drawdown = abs(float(drawdown.min())) if not drawdown.empty else 0.0

        returns = equity.pct_change().dropna()
        sharpe: float | None = None
        if len(returns) > 1 and float(returns.std()) > 0:
            sharpe = float(np.sqrt(252) * returns.mean() / returns.std())

        total_return = (final_equity / self.starting_cash - 1) * 100
        return BacktestResult(
            symbol=symbol,
            starting_cash=self.starting_cash,
            final_equity=float(final_equity),
            total_return_pct=total_return,
            buy_hold_return_pct=buy_hold_return_pct,
            alpha_vs_buy_hold_pct=total_return - buy_hold_return_pct,
            trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate_pct=(len(wins) / len(trades) * 100) if trades else 0.0,
            profit_factor=profit_factor,
            max_drawdown_pct=max_drawdown,
            sharpe_ratio=sharpe,
            average_trade=float(np.mean(pnls)) if pnls else 0.0,
            best_trade=max(pnls) if pnls else 0.0,
            worst_trade=min(pnls) if pnls else 0.0,
            total_fees=sum(trade.entry_fee + trade.exit_fee for trade in trades),
        )


def contributors(strategy_name: str) -> list[str]:
    match = re.fullmatch(r"ensemble\[(.*)]", strategy_name)
    if not match:
        return [strategy_name]
    return [
        item.strip()
        for item in match.group(1).split(",")
        if item.strip()
    ]


def breakdown_frame(
    trades: list[BacktestTrade],
    *,
    mode: str,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []

    if mode == "side":
        groups: dict[str, list[BacktestTrade]] = {}
        for trade in trades:
            groups.setdefault(trade.side, []).append(trade)
    elif mode == "strategy":
        groups = {}
        for trade in trades:
            for name in contributors(trade.strategy):
                groups.setdefault(name, []).append(trade)
    else:
        raise ValueError("mode muss 'side' oder 'strategy' sein.")

    for name, group in sorted(groups.items()):
        pnls = [trade.pnl for trade in group]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_loss = abs(sum(losses))
        profit_factor = (
            sum(wins) / gross_loss
            if gross_loss > 0
            else (float("inf") if wins else None)
        )
        rows.append(
            {
                mode: name,
                "trades": len(group),
                "wins": len(wins),
                "win_rate_pct": len(wins) / len(group) * 100 if group else 0.0,
                "net_pnl": sum(pnls),
                "average_trade": float(np.mean(pnls)) if pnls else 0.0,
                "profit_factor": profit_factor,
                "fees": sum(t.entry_fee + t.exit_fee for t in group),
            }
        )

    return pd.DataFrame(rows)


def walk_forward(
    *,
    symbol: str,
    raw_data: pd.DataFrame,
    backtester: Backtester,
    folds: int,
    warmup_rows: int = 140,
) -> list[WalkForwardFold]:
    if folds < 2:
        raise ValueError("Mindestens 2 Walk-Forward-Folds erforderlich.")
    if len(raw_data) < warmup_rows + folds * 30:
        return []

    usable_start = warmup_rows
    boundaries = np.linspace(usable_start, len(raw_data), folds + 1, dtype=int)
    output: list[WalkForwardFold] = []

    for fold in range(folds):
        test_start_pos = int(boundaries[fold])
        test_end_pos = int(boundaries[fold + 1])
        slice_start = max(0, test_start_pos - warmup_rows)
        window = raw_data.iloc[slice_start:test_end_pos].copy()
        test_start = raw_data.index[test_start_pos]

        try:
            result, _, _ = backtester.run(
                symbol,
                window,
                trade_start=pd.Timestamp(test_start),
            )
        except ValueError:
            continue

        output.append(
            WalkForwardFold(
                symbol=symbol,
                fold=fold + 1,
                start=str(test_start),
                end=str(raw_data.index[test_end_pos - 1]),
                return_pct=result.total_return_pct,
                buy_hold_return_pct=result.buy_hold_return_pct,
                trades=result.trades,
                profit_factor=result.profit_factor,
                max_drawdown_pct=result.max_drawdown_pct,
            )
        )

    return output


def save_outputs(
    *,
    all_results: list[BacktestResult],
    all_trades: list[BacktestTrade],
    curves: dict[str, pd.DataFrame],
    walk_forward_rows: list[WalkForwardFold],
) -> None:
    output_dir = BASE_DIR / "backtest_output"
    output_dir.mkdir(exist_ok=True)

    pd.DataFrame([asdict(result) for result in all_results]).to_csv(
        output_dir / "summary.csv",
        index=False,
    )
    pd.DataFrame([asdict(trade) for trade in all_trades]).to_csv(
        output_dir / "trades.csv",
        index=False,
    )
    breakdown_frame(all_trades, mode="side").to_csv(
        output_dir / "breakdown_by_side.csv",
        index=False,
    )
    breakdown_frame(all_trades, mode="strategy").to_csv(
        output_dir / "breakdown_by_strategy.csv",
        index=False,
    )
    pd.DataFrame([asdict(row) for row in walk_forward_rows]).to_csv(
        output_dir / "walk_forward.csv",
        index=False,
    )

    for symbol, curve in curves.items():
        safe_name = symbol.replace("=", "_").replace("^", "_")
        curve.to_csv(output_dir / f"equity_{safe_name}.csv")

    (output_dir / "summary.json").write_text(
        json.dumps([asdict(result) for result in all_results], indent=2),
        encoding="utf-8",
    )


def format_number(value: float | None) -> str:
    if value is None:
        return "-"
    if math.isinf(value):
        return "∞"
    return f"{value:.2f}"


def print_result(result: BacktestResult) -> None:
    print("\n" + "=" * 64)
    print(f"BACKTEST: {result.symbol}")
    print(f"Startkapital:         {result.starting_cash:10.2f} €")
    print(f"Endkapital:           {result.final_equity:10.2f} €")
    print(f"Bot-Rendite:          {result.total_return_pct:10.2f} %")
    print(f"Buy-and-Hold:         {result.buy_hold_return_pct:10.2f} %")
    print(f"Mehr-/Minderrendite:  {result.alpha_vs_buy_hold_pct:10.2f} %-Pkt.")
    print(f"Trades:               {result.trades:10d}")
    print(f"Trefferquote:         {result.win_rate_pct:10.2f} %")
    print(f"Profit Factor:        {format_number(result.profit_factor):>10}")
    print(f"Max. Drawdown:        {result.max_drawdown_pct:10.2f} %")
    print(f"Sharpe Ratio:         {format_number(result.sharpe_ratio):>10}")
    print(f"Gesamte Gebühren:     {result.total_fees:10.2f} €")
    print(f"Ø Trade netto:        {result.average_trade:10.2f} €")
    print(f"Bester Trade:         {result.best_trade:10.2f} €")
    print(f"Schlechtester:        {result.worst_trade:10.2f} €")
    print("=" * 64)


def parse_symbols(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return settings.symbols
    symbols = tuple(item.strip().upper() for item in raw.split(",") if item.strip())
    if not symbols:
        raise ValueError("Mindestens ein Symbol angeben.")
    return symbols


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtesting für AI Trading Bot")
    parser.add_argument("--symbols", type=str, default=None)
    parser.add_argument("--period", type=str, default="5y")
    parser.add_argument("--interval", type=str, default="1d")
    parser.add_argument("--walk-forward-folds", type=int, default=4)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbols = parse_symbols(args.symbols)

    data_source = YahooMarketData()
    strategy_engine = StrategyEngine(
        settings.min_signal_confidence,
        settings.min_strategy_agreement,
    )
    risk_manager = RiskManager(
        settings.risk_per_trade,
        settings.max_position_fraction,
    )
    backtester = Backtester(
        strategy_engine=strategy_engine,
        risk_manager=risk_manager,
        starting_cash=settings.starting_cash,
        fee_rate=settings.fee_rate,
        slippage_rate=settings.slippage_rate,
    )

    all_results: list[BacktestResult] = []
    all_trades: list[BacktestTrade] = []
    curves: dict[str, pd.DataFrame] = {}
    walk_forward_rows: list[WalkForwardFold] = []

    for symbol in symbols:
        print(f"\nLade historische Daten für {symbol} ...")
        try:
            raw_data = data_source.fetch(
                symbol,
                period=args.period,
                interval=args.interval,
            )
            result, trades, curve = backtester.run(symbol, raw_data)
        except Exception as exc:
            print(f"{symbol}: Backtest fehlgeschlagen: {exc}")
            continue

        print_result(result)
        all_results.append(result)
        all_trades.extend(trades)
        curves[symbol] = curve
        walk_forward_rows.extend(
            walk_forward(
                symbol=symbol,
                raw_data=raw_data,
                backtester=backtester,
                folds=args.walk_forward_folds,
            )
        )

    if not all_results:
        raise SystemExit("Kein Backtest konnte abgeschlossen werden.")

    save_outputs(
        all_results=all_results,
        all_trades=all_trades,
        curves=curves,
        walk_forward_rows=walk_forward_rows,
    )
    print(f"\nErgebnisse gespeichert in: {BASE_DIR / 'backtest_output'}")


if __name__ == "__main__":
    main()
