from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import threading
from typing import Any, Callable

import pandas as pd
import websocket


LOGGER = logging.getLogger(__name__)
STREAM_URL_TEMPLATE = "wss://stream.data.alpaca.markets/v2/{feed}"


@dataclass(frozen=True, slots=True)
class AlpacaTradeTick:
    symbol: str
    price: float
    timestamp: pd.Timestamp
    size: float
    exchange: str = ""


@dataclass(frozen=True, slots=True)
class AlpacaMinuteBar:
    symbol: str
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


def to_alpaca_symbol(symbol: str) -> str:
    raw = str(symbol).strip().upper()
    if raw in {"BRK-A", "BRK-B"}:
        return raw.replace("-", ".")
    return raw


def from_alpaca_symbol(symbol: str) -> str:
    raw = str(symbol).strip().upper()
    if raw in {"BRK.A", "BRK.B"}:
        return raw.replace(".", "-")
    return raw


def _timestamp(value: object) -> pd.Timestamp | None:
    if value in {None, ""}:
        return None
    try:
        stamp = pd.to_datetime(value, utc=True)
    except (TypeError, ValueError):
        return None
    if isinstance(stamp, pd.DatetimeIndex):
        return None
    return pd.Timestamp(stamp).tz_convert(None)


def parse_trade(message: dict[str, Any]) -> AlpacaTradeTick | None:
    if str(message.get("T", "")) != "t":
        return None
    try:
        symbol = from_alpaca_symbol(message.get("S", ""))
        price = float(message.get("p", 0))
        size = float(message.get("s", 0))
        stamp = _timestamp(message.get("t"))
    except (TypeError, ValueError):
        return None
    if not symbol or price <= 0 or stamp is None:
        return None
    return AlpacaTradeTick(
        symbol=symbol,
        price=price,
        timestamp=stamp,
        size=max(size, 0.0),
        exchange=str(message.get("x", "")),
    )


def parse_bar(message: dict[str, Any]) -> AlpacaMinuteBar | None:
    if str(message.get("T", "")) not in {"b", "u"}:
        return None
    try:
        symbol = from_alpaca_symbol(message.get("S", ""))
        stamp = _timestamp(message.get("t"))
        open_price = float(message.get("o", 0))
        high = float(message.get("h", 0))
        low = float(message.get("l", 0))
        close = float(message.get("c", 0))
        volume = float(message.get("v", 0))
    except (TypeError, ValueError):
        return None
    if not symbol or stamp is None or min(open_price, high, low, close) <= 0:
        return None
    return AlpacaMinuteBar(
        symbol=symbol,
        timestamp=stamp.floor("min"),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=max(volume, 0.0),
    )


class AlpacaStockStream:
    """Small WebSocket client for Alpaca stock trades and completed minute bars."""

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        symbols: tuple[str, ...],
        feed: str,
        on_trade: Callable[[AlpacaTradeTick], None],
        on_bar: Callable[[AlpacaMinuteBar], None],
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.secret_key = secret_key.strip()
        self.symbols = tuple(dict.fromkeys(str(item).upper() for item in symbols if item))
        self.stream_symbols = tuple(to_alpaca_symbol(item) for item in self.symbols)
        self.feed = feed.strip().lower() or "iex"
        self.on_trade_callback = on_trade
        self.on_bar_callback = on_bar
        self.on_ready_callback = on_ready
        self.url = STREAM_URL_TEMPLATE.format(feed=self.feed)
        self.ws: websocket.WebSocketApp | None = None
        self.last_error = ""
        self.closed = threading.Event()

        if not self.api_key or not self.secret_key:
            raise ValueError("Alpaca-Paper-Keys fehlen.")
        if not self.symbols:
            raise ValueError("Für den Alpaca-WebSocket fehlen Symbole.")
        if self.feed not in {"iex", "sip", "delayed_sip"}:
            raise ValueError("ALPACA_DATA_FEED muss iex, sip oder delayed_sip sein.")

    def run(self) -> None:
        self.closed.clear()
        self.ws = websocket.WebSocketApp(
            self.url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self.ws.run_forever(
            ping_interval=20,
            ping_timeout=10,
            skip_utf8_validation=True,
        )

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                LOGGER.exception("Alpaca-WebSocket konnte nicht sauber geschlossen werden.")
        self.closed.set()

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        ws.send(
            json.dumps(
                {
                    "action": "auth",
                    "key": self.api_key,
                    "secret": self.secret_key,
                }
            )
        )

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.last_error = "Alpaca-WebSocket lieferte ungültiges JSON."
            LOGGER.warning(self.last_error)
            return

        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_type = str(message.get("T", ""))

            if message_type == "success":
                if str(message.get("msg", "")) == "authenticated":
                    ws.send(
                        json.dumps(
                            {
                                "action": "subscribe",
                                "trades": list(self.stream_symbols),
                                "bars": list(self.stream_symbols),
                            }
                        )
                    )
                continue

            if message_type == "subscription":
                self.last_error = ""
                if self.on_ready_callback is not None:
                    self.on_ready_callback()
                continue

            if message_type == "error":
                code = message.get("code", "-")
                detail = str(message.get("msg", "Unbekannter WebSocket-Fehler"))
                self.last_error = f"Alpaca-WebSocket {code}: {detail}"
                LOGGER.error(self.last_error)
                ws.close()
                continue

            trade = parse_trade(message)
            if trade is not None:
                try:
                    self.on_trade_callback(trade)
                except Exception:
                    LOGGER.exception("Fehler beim Verarbeiten eines Alpaca-Trades.")
                continue

            bar = parse_bar(message)
            if bar is not None:
                try:
                    self.on_bar_callback(bar)
                except Exception:
                    LOGGER.exception("Fehler beim Verarbeiten einer Alpaca-Minutenkerze.")

    def _on_error(self, _ws: websocket.WebSocketApp, error: object) -> None:
        self.last_error = f"Alpaca-WebSocket: {error}"
        LOGGER.error(self.last_error)

    def _on_close(
        self,
        _ws: websocket.WebSocketApp,
        status_code: int | None,
        message: str | None,
    ) -> None:
        self.closed.set()
        if status_code and not self.last_error:
            self.last_error = f"Alpaca-WebSocket geschlossen ({status_code}): {message or '-'}"
