from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import math

import pandas as pd


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    direction: Direction
    confidence: float
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    strategy: str
    reason: str

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence muss zwischen 0 und 1 liegen.")
        for value in (self.entry_price, self.stop_loss, self.take_profit):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("Preise müssen positive, endliche Zahlen sein.")


def hold(symbol: str, price: float, strategy: str, reason: str) -> Signal:
    return Signal(
        symbol=symbol,
        direction=Direction.HOLD,
        confidence=0.0,
        entry_price=price,
        stop_loss=None,
        take_profit=None,
        strategy=strategy,
        reason=reason,
    )


def trend_signal(symbol: str, data: pd.DataFrame) -> Signal:
    row = data.iloc[-1]
    price = float(row["close"])
    atr = float(row["atr_14"])
    volume_ok = row["volume"] >= row["volume_sma_20"] * 0.9

    if row["sma_20"] > row["sma_50"] and row["macd"] > row["macd_signal"] and volume_ok:
        strength = min((row["sma_20"] / row["sma_50"] - 1) * 12, 0.25)
        return Signal(
            symbol,
            Direction.LONG,
            min(0.60 + float(strength), 0.88),
            price,
            max(price - 2 * atr, price * 0.90),
            price + 3 * atr,
            "trend",
            "SMA20 über SMA50, MACD und Volumen bestätigen.",
        )

    if row["sma_20"] < row["sma_50"] and row["macd"] < row["macd_signal"] and volume_ok:
        strength = min((row["sma_50"] / row["sma_20"] - 1) * 12, 0.25)
        return Signal(
            symbol,
            Direction.SHORT,
            min(0.60 + float(strength), 0.88),
            price,
            price + 2 * atr,
            max(price - 3 * atr, price * 0.01),
            "trend",
            "SMA20 unter SMA50, MACD und Volumen bestätigen.",
        )

    return hold(symbol, price, "trend", "Kein bestätigter Trend.")


def momentum_signal(symbol: str, data: pd.DataFrame) -> Signal:
    row = data.iloc[-1]
    price = float(row["close"])
    atr = float(row["atr_14"])
    momentum = float(row["return_20"])
    current_rsi = float(row["rsi_14"])

    if momentum > 0.04 and 52 <= current_rsi <= 72:
        return Signal(
            symbol,
            Direction.LONG,
            min(0.58 + momentum * 1.8, 0.86),
            price,
            max(price - 1.8 * atr, price * 0.90),
            price + 2.8 * atr,
            "momentum",
            f"20-Perioden-Momentum {momentum:.1%}, RSI {current_rsi:.1f}.",
        )

    if momentum < -0.04 and 28 <= current_rsi <= 48:
        return Signal(
            symbol,
            Direction.SHORT,
            min(0.58 + abs(momentum) * 1.8, 0.86),
            price,
            price + 1.8 * atr,
            max(price - 2.8 * atr, price * 0.01),
            "momentum",
            f"Negatives Momentum {momentum:.1%}, RSI {current_rsi:.1f}.",
        )

    return hold(symbol, price, "momentum", "Momentum nicht stark genug.")


def mean_reversion_signal(symbol: str, data: pd.DataFrame) -> Signal:
    row = data.iloc[-1]
    price = float(row["close"])
    atr = float(row["atr_14"])
    zscore = float(row["zscore_20"])
    current_rsi = float(row["rsi_14"])

    if zscore <= -2 and current_rsi <= 35:
        return Signal(
            symbol,
            Direction.LONG,
            min(0.60 + (abs(zscore) - 2) * 0.08, 0.84),
            price,
            max(price - 1.5 * atr, price * 0.90),
            float(row["sma_20"]),
            "mean_reversion",
            f"Unter Mittelwert: Z-Score {zscore:.2f}, RSI {current_rsi:.1f}.",
        )

    if zscore >= 2 and current_rsi >= 65:
        return Signal(
            symbol,
            Direction.SHORT,
            min(0.60 + (abs(zscore) - 2) * 0.08, 0.84),
            price,
            price + 1.5 * atr,
            float(row["sma_20"]),
            "mean_reversion",
            f"Über Mittelwert: Z-Score {zscore:.2f}, RSI {current_rsi:.1f}.",
        )

    return hold(symbol, price, "mean_reversion", "Keine starke Übertreibung.")


def breakout_signal(symbol: str, data: pd.DataFrame) -> Signal:
    row = data.iloc[-1]
    price = float(row["close"])
    atr = float(row["atr_14"])
    volume_ratio = float(row["volume"] / max(row["volume_sma_20"], 1e-12))

    if price > row["high_20"] and volume_ratio >= 1.15:
        return Signal(
            symbol,
            Direction.LONG,
            min(0.62 + (volume_ratio - 1.15) * 0.12, 0.88),
            price,
            max(price - 1.6 * atr, price * 0.90),
            price + 3.2 * atr,
            "breakout",
            f"Aufwärtsausbruch mit Volumenfaktor {volume_ratio:.2f}.",
        )

    if price < row["low_20"] and volume_ratio >= 1.15:
        return Signal(
            symbol,
            Direction.SHORT,
            min(0.62 + (volume_ratio - 1.15) * 0.12, 0.88),
            price,
            price + 1.6 * atr,
            max(price - 3.2 * atr, price * 0.01),
            "breakout",
            f"Abwärtsausbruch mit Volumenfaktor {volume_ratio:.2f}.",
        )

    return hold(symbol, price, "breakout", "Kein bestätigter Ausbruch.")


class StrategyEngine:
    def __init__(self, min_confidence: float, min_agreement: int) -> None:
        self.min_confidence = min_confidence
        self.min_agreement = min_agreement

    def analyze(self, symbol: str, data: pd.DataFrame) -> Signal:
        signals = [
            trend_signal(symbol, data),
            momentum_signal(symbol, data),
            mean_reversion_signal(symbol, data),
            breakout_signal(symbol, data),
        ]
        actionable = [
            signal for signal in signals
            if signal.direction is not Direction.HOLD
            and signal.confidence >= self.min_confidence
        ]
        price = float(data.iloc[-1]["close"])

        if not actionable:
            return hold(symbol, price, "ensemble", "Keine Strategie erfüllt die Schwelle.")

        counts = Counter(signal.direction for signal in actionable)
        direction, agreement = counts.most_common(1)[0]
        if agreement < self.min_agreement:
            return hold(
                symbol,
                price,
                "ensemble",
                f"Nur {agreement} Strategien stimmen überein.",
            )

        matching = [signal for signal in actionable if signal.direction is direction]
        confidence = sum(s.confidence for s in matching) / len(matching)
        stops = [s.stop_loss for s in matching if s.stop_loss is not None]
        targets = [s.take_profit for s in matching if s.take_profit is not None]

        stop_loss = max(stops) if direction is Direction.LONG else min(stops)
        take_profit = sum(targets) / len(targets)
        names = ", ".join(s.strategy for s in matching)
        reasons = " | ".join(s.reason for s in matching)

        return Signal(
            symbol=symbol,
            direction=direction,
            confidence=min(float(confidence), 0.95),
            entry_price=price,
            stop_loss=float(stop_loss),
            take_profit=float(take_profit),
            strategy=f"ensemble[{names}]",
            reason=reasons,
        )
