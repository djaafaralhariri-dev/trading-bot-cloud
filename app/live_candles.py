from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import Callable

import pandas as pd

from app.market_data import YahooMarketData


LOGGER = logging.getLogger(__name__)
BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True, slots=True)
class LiveTick:
    symbol: str
    price: float
    timestamp: pd.Timestamp
    volume_delta: float
    currency: str = ""
    exchange: str = ""


def parse_yahoo_message(
    message: dict,
    previous_cumulative_volume: float | None = None,
) -> tuple[LiveTick | None, float | None]:
    """Normalisiert eine von yfinance dekodierte Yahoo-WebSocket-Nachricht."""
    try:
        symbol = str(message.get("id", "")).strip().upper()
        price = float(message.get("price", 0))
        raw_time = float(message.get("time", time.time()))
    except (TypeError, ValueError):
        return None, previous_cumulative_volume

    if not symbol or price <= 0:
        return None, previous_cumulative_volume

    # Yahoo kann Sekunden oder Millisekunden liefern.
    seconds = raw_time / 1000 if raw_time > 10_000_000_000 else raw_time
    timestamp = pd.to_datetime(seconds, unit="s", utc=True).tz_convert(None)

    last_size = _safe_float(message.get("last_size"))
    cumulative = _first_positive(
        message.get("day_volume"),
        message.get("vol_24hr"),
        message.get("vol_all_currencies"),
    )

    if last_size > 0:
        volume_delta = last_size
    elif cumulative is not None and previous_cumulative_volume is not None:
        volume_delta = max(cumulative - previous_cumulative_volume, 0.0)
    else:
        volume_delta = 0.0

    return (
        LiveTick(
            symbol=symbol,
            price=price,
            timestamp=timestamp,
            volume_delta=volume_delta,
            currency=str(message.get("currency", "")),
            exchange=str(message.get("exchange", "")),
        ),
        cumulative if cumulative is not None else previous_cumulative_volume,
    )


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first_positive(*values: object) -> float | None:
    for value in values:
        number = _safe_float(value)
        if number > 0:
            return number
    return None


class MinuteCandleStore:
    def __init__(
        self,
        *,
        symbols: tuple[str, ...],
        data_dir: Path,
        max_bars: int,
        on_completed_bar: Callable[[str, pd.DataFrame], None] | None = None,
    ) -> None:
        self.symbols = symbols
        self.data_dir = data_dir
        self.max_bars = max_bars
        self.on_completed_bar = on_completed_bar
        self.frames: dict[str, pd.DataFrame] = {}
        self.current: dict[str, dict[str, float | pd.Timestamp]] = {}
        self.previous_cumulative_volume: dict[str, float] = {}
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def bootstrap(
        self,
        *,
        period: str,
        interval: str,
    ) -> None:
        source = YahooMarketData()
        for symbol in self.symbols:
            try:
                frame = source.fetch(symbol, period=period, interval=interval)
                frame = frame.tail(self.max_bars).copy()
                self.frames[symbol] = frame
                self._save(symbol)
                LOGGER.info(
                    "%s: %d historische Minutenkerzen geladen.",
                    symbol,
                    len(frame),
                )
            except Exception:
                LOGGER.exception("%s: Minutenhistorie konnte nicht geladen werden.", symbol)
                self.frames[symbol] = pd.DataFrame(columns=BAR_COLUMNS)

    def handle_message(self, message: dict) -> LiveTick | None:
        symbol = str(message.get("id", "")).strip().upper()
        previous = self.previous_cumulative_volume.get(symbol)
        tick, cumulative = parse_yahoo_message(message, previous)
        if tick is None or tick.symbol not in self.symbols:
            return None

        if cumulative is not None:
            self.previous_cumulative_volume[tick.symbol] = cumulative

        self.handle_tick(tick)
        return tick

    def handle_tick(self, tick: LiveTick) -> None:
        minute = tick.timestamp.floor("min")
        current = self.current.get(tick.symbol)

        if current is None:
            self.current[tick.symbol] = self._new_candle(minute, tick)
            return

        current_minute = pd.Timestamp(current["time"])

        if minute < current_minute:
            # Veraltete Nachricht nicht in neuere Kerze mischen.
            return

        if minute == current_minute:
            current["high"] = max(float(current["high"]), tick.price)
            current["low"] = min(float(current["low"]), tick.price)
            current["close"] = tick.price
            current["volume"] = float(current["volume"]) + tick.volume_delta
            return

        self._finalize(tick.symbol, current)
        self.current[tick.symbol] = self._new_candle(minute, tick)

    def force_finalize_stale(self, now: pd.Timestamp | None = None) -> None:
        """Schließt Kerzen, wenn seit mindestens einer Minute kein neuer Tick kam."""
        now = now or pd.Timestamp.utcnow().tz_localize(None)
        for symbol, candle in list(self.current.items()):
            candle_time = pd.Timestamp(candle["time"])
            if now.floor("min") > candle_time:
                self._finalize(symbol, candle)
                del self.current[symbol]

    def append_completed_bar(
        self,
        *,
        symbol: str,
        timestamp: pd.Timestamp,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        """Speichert eine bereits fertige 1-Minuten-Kerze, etwa von Alpaca."""
        if symbol not in self.symbols:
            return
        self._finalize(
            symbol,
            {
                "time": pd.Timestamp(timestamp).floor("min"),
                "open": float(open_price),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": max(float(volume), 0.0),
            },
        )

    def latest_frame(self, symbol: str) -> pd.DataFrame:
        return self.frames.get(symbol, pd.DataFrame(columns=BAR_COLUMNS)).copy()

    @staticmethod
    def _new_candle(
        minute: pd.Timestamp,
        tick: LiveTick,
    ) -> dict[str, float | pd.Timestamp]:
        return {
            "time": minute,
            "open": tick.price,
            "high": tick.price,
            "low": tick.price,
            "close": tick.price,
            "volume": tick.volume_delta,
        }

    def _finalize(
        self,
        symbol: str,
        candle: dict[str, float | pd.Timestamp],
    ) -> None:
        timestamp = pd.Timestamp(candle["time"])
        row = pd.DataFrame(
            {
                "open": [float(candle["open"])],
                "high": [float(candle["high"])],
                "low": [float(candle["low"])],
                "close": [float(candle["close"])],
                "volume": [float(candle["volume"])],
            },
            index=[timestamp],
        )

        frame = self.frames.get(symbol)
        if frame is None or frame.empty:
            frame = row
        else:
            frame = pd.concat([frame, row])
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()

        frame = frame.tail(self.max_bars)
        self.frames[symbol] = frame
        self._save(symbol)

        if self.on_completed_bar is not None:
            self.on_completed_bar(symbol, frame.copy())

    def _save(self, symbol: str) -> None:
        safe = symbol.replace("=", "_").replace("^", "_").replace("/", "_")
        path = self.data_dir / f"{safe}_1m.csv"
        temp = path.with_suffix(".tmp")
        self.frames[symbol].to_csv(temp, index_label="time")
        temp.replace(path)
