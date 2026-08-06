from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Mapping

from app.strategies import Direction, Signal


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Position:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    reserved_cash: float
    strategy: str
    reason: str
    opened_at: str

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == Direction.LONG.value:
            return (current_price - self.entry_price) * self.quantity
        return (self.entry_price - current_price) * self.quantity


class PaperBroker:
    def __init__(
        self,
        starting_cash: float,
        fee_rate: float,
        slippage_rate: float,
        state_path: Path,
    ) -> None:
        self.starting_cash = float(starting_cash)
        self.fee_rate = float(fee_rate)
        self.slippage_rate = float(slippage_rate)
        self.state_path = state_path
        self.cash = self.starting_cash
        self.realized_pnl = 0.0
        self.positions: dict[str, Position] = {}
        self.trade_history: list[dict[str, object]] = []
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.cash = float(payload["cash"])
            self.realized_pnl = float(payload.get("realized_pnl", 0.0))
            self.positions = {
                symbol: Position(**position)
                for symbol, position in payload.get("positions", {}).items()
            }
            self.trade_history = list(payload.get("trade_history", []))
        except Exception:
            LOGGER.exception("Paper-State konnte nicht geladen werden.")
            self.cash = self.starting_cash
            self.realized_pnl = 0.0
            self.positions = {}
            self.trade_history = []

    def _save_state(self) -> None:
        payload = {
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "positions": {
                symbol: asdict(position)
                for symbol, position in self.positions.items()
            },
            "trade_history": self.trade_history,
        }
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    def reset(self) -> None:
        self.cash = self.starting_cash
        self.realized_pnl = 0.0
        self.positions = {}
        self.trade_history = []
        if self.state_path.exists():
            self.state_path.unlink()

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def equity(self, prices: Mapping[str, float] | None = None) -> float:
        prices = prices or {}
        open_value = 0.0
        for symbol, position in self.positions.items():
            current = float(prices.get(symbol, position.entry_price))
            open_value += position.reserved_cash + position.unrealized_pnl(current)
        return self.cash + open_value

    def open_position(self, signal: Signal, quantity: float) -> Position | None:
        if signal.symbol in self.positions:
            return None
        if signal.direction is Direction.HOLD:
            return None
        if signal.stop_loss is None or signal.take_profit is None:
            return None

        adverse_slippage = (
            1 + self.slippage_rate
            if signal.direction is Direction.LONG
            else 1 - self.slippage_rate
        )
        fill_price = signal.entry_price * adverse_slippage
        notional = fill_price * quantity
        entry_fee = notional * self.fee_rate
        required_cash = notional + entry_fee

        if required_cash > self.cash:
            LOGGER.warning(
                "%s: zu wenig Cash. Benötigt %.2f, vorhanden %.2f.",
                signal.symbol,
                required_cash,
                self.cash,
            )
            return None

        self.cash -= required_cash
        position = Position(
            symbol=signal.symbol,
            side=signal.direction.value,
            quantity=quantity,
            entry_price=fill_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            reserved_cash=notional,
            strategy=signal.strategy,
            reason=signal.reason,
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
        self.positions[signal.symbol] = position
        self.trade_history.append(
            {
                "event": "open",
                "time": position.opened_at,
                "symbol": position.symbol,
                "side": position.side,
                "quantity": position.quantity,
                "price": position.entry_price,
                "fee": entry_fee,
                "strategy": position.strategy,
            }
        )
        self._save_state()
        return position

    def close_position(
        self,
        symbol: str,
        market_price: float,
        close_reason: str,
    ) -> float:
        position = self.positions[symbol]
        adverse_slippage = (
            1 - self.slippage_rate
            if position.side == Direction.LONG.value
            else 1 + self.slippage_rate
        )
        fill_price = market_price * adverse_slippage
        gross_pnl = position.unrealized_pnl(fill_price)
        exit_fee = fill_price * position.quantity * self.fee_rate
        net_pnl = gross_pnl - exit_fee

        self.cash += position.reserved_cash + net_pnl
        self.realized_pnl += net_pnl
        self.trade_history.append(
            {
                "event": "close",
                "time": datetime.now(timezone.utc).isoformat(),
                "symbol": position.symbol,
                "side": position.side,
                "quantity": position.quantity,
                "price": fill_price,
                "fee": exit_fee,
                "pnl": net_pnl,
                "reason": close_reason,
            }
        )
        del self.positions[symbol]
        self._save_state()
        return net_pnl

    def update_positions(
        self,
        prices: Mapping[str, float],
    ) -> list[tuple[str, float, str]]:
        closed: list[tuple[str, float, str]] = []
        for symbol in list(self.positions):
            if symbol not in prices:
                continue

            position = self.positions[symbol]
            price = float(prices[symbol])

            if position.side == Direction.LONG.value:
                if price <= position.stop_loss:
                    reason = "stop_loss"
                elif price >= position.take_profit:
                    reason = "take_profit"
                else:
                    continue
            else:
                if price >= position.stop_loss:
                    reason = "stop_loss"
                elif price <= position.take_profit:
                    reason = "take_profit"
                else:
                    continue

            pnl = self.close_position(symbol, price, reason)
            closed.append((symbol, pnl, reason))
        return closed
