from __future__ import annotations

from dataclasses import dataclass
import math

from app.strategies import Direction, Signal


@dataclass(frozen=True, slots=True)
class PositionPlan:
    quantity: float
    notional: float
    risk_amount: float
    risk_fraction: float


class RiskManager:
    def __init__(
        self,
        risk_per_trade: float,
        max_position_fraction: float,
        min_notional: float = 1.0,
    ) -> None:
        if not 0 < risk_per_trade <= 0.05:
            raise ValueError("risk_per_trade muss zwischen 0 und 5 % liegen.")
        if not 0 < max_position_fraction <= 1:
            raise ValueError("max_position_fraction muss zwischen 0 und 1 liegen.")
        self.risk_per_trade = risk_per_trade
        self.max_position_fraction = max_position_fraction
        self.min_notional = min_notional

    def plan(
        self,
        signal: Signal,
        equity: float,
        available_cash: float,
    ) -> PositionPlan | None:
        if signal.direction is Direction.HOLD or signal.stop_loss is None:
            return None
        if equity <= 0 or available_cash <= 0:
            return None

        stop_distance = abs(signal.entry_price - signal.stop_loss)
        if not math.isfinite(stop_distance) or stop_distance <= 0:
            return None

        confidence_multiplier = 0.75 + 0.5 * signal.confidence
        risk_budget = equity * self.risk_per_trade * confidence_multiplier

        quantity_by_risk = risk_budget / stop_distance
        max_notional = min(
            equity * self.max_position_fraction,
            available_cash,
        )
        quantity_by_cash = max_notional / signal.entry_price

        quantity = min(quantity_by_risk, quantity_by_cash)
        notional = quantity * signal.entry_price
        actual_risk = quantity * stop_distance

        if not math.isfinite(quantity) or quantity <= 0 or notional < self.min_notional:
            return None

        return PositionPlan(
            quantity=float(quantity),
            notional=float(notional),
            risk_amount=float(actual_risk),
            risk_fraction=float(actual_risk / equity),
        )
