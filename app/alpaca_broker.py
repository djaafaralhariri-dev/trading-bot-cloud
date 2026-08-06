from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import time
from typing import Any, Mapping
from urllib.parse import quote
import uuid

import requests

from app.paper_broker import Position
from app.strategies import Direction, Signal


LOGGER = logging.getLogger(__name__)
PAPER_BASE_URL = "https://paper-api.alpaca.markets"
NON_US_SUFFIXES = (
    ".DE", ".PA", ".AS", ".SW", ".L", ".T", ".KS", ".HK", ".TW", ".AX"
)


class AlpacaAPIError(RuntimeError):
    pass


class AlpacaPaperBroker:
    """Small Alpaca Paper API adapter.

    The endpoint is intentionally hard-coded to Alpaca's paper domain. There is
    no live-domain switch in this class.
    """

    broker_name = "alpaca_paper"

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        state_path: Path,
        order_execution_enabled: bool,
        allow_shorts: bool,
        max_order_notional: float,
        refresh_seconds: int = 10,
        require_fractionable: bool = True,
        session: requests.Session | None = None,
    ) -> None:
        api_key = api_key.strip()
        secret_key = secret_key.strip()
        if not api_key or not secret_key:
            raise ValueError(
                "Alpaca-Paper-Keys fehlen. Starte zuerst setup_alpaca_keys.bat."
            )
        if max_order_notional <= 0:
            raise ValueError("ALPACA_MAX_ORDER_NOTIONAL muss größer als 0 sein.")

        self.api_key = api_key
        self.secret_key = secret_key
        self.state_path = state_path
        self.order_execution_enabled = bool(order_execution_enabled)
        self.allow_shorts = bool(allow_shorts)
        self.max_order_notional = float(max_order_notional)
        self.refresh_seconds = max(2, int(refresh_seconds))
        self.require_fractionable = bool(require_fractionable)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "ai-trading-bot-paper/1.0",
            }
        )

        self.account: dict[str, Any] = {}
        self.positions: dict[str, Position] = {}
        self.trade_history: list[dict[str, Any]] = []
        self.realized_pnl = 0.0
        self.pending_open: dict[str, dict[str, Any]] = {}
        self.pending_close: dict[str, dict[str, Any]] = {}
        self.position_metadata: dict[str, dict[str, Any]] = {}
        self.asset_cache: dict[str, dict[str, Any] | None] = {}
        self.last_refresh = 0.0
        self.last_error = ""
        self._load_state()
        self.refresh(force=True)

    @property
    def cash(self) -> float:
        return _to_float(self.account.get("cash"))

    @property
    def account_number_masked(self) -> str:
        raw = str(self.account.get("account_number", ""))
        if len(raw) <= 4:
            return raw or "-"
        return f"***{raw[-4:]}"

    @property
    def account_status(self) -> str:
        return str(self.account.get("status", "-"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: int = 20,
    ) -> Any:
        url = f"{PAPER_BASE_URL}{path}"
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise AlpacaAPIError(f"Alpaca-Verbindung fehlgeschlagen: {exc}") from exc

        request_id = response.headers.get("X-Request-ID", "")
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {"message": response.text[:500]}

        if not response.ok:
            message = ""
            if isinstance(data, dict):
                message = str(data.get("message") or data.get("error") or "")
            suffix = f" | Request-ID {request_id}" if request_id else ""
            raise AlpacaAPIError(
                f"Alpaca HTTP {response.status_code}: {message or response.text[:300]}{suffix}"
            )
        return data

    def refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_refresh < self.refresh_seconds:
            return

        account = self._request("GET", "/v2/account")
        broker_positions = self._request("GET", "/v2/positions")
        open_orders = self._request(
            "GET",
            "/v2/orders",
            params={"status": "open", "limit": 500, "direction": "desc"},
        )

        if not isinstance(account, dict):
            raise AlpacaAPIError("Alpaca lieferte ungültige Kontodaten.")
        if not isinstance(broker_positions, list):
            broker_positions = []
        if not isinstance(open_orders, list):
            open_orders = []

        self.account = account
        self.realized_pnl = _to_float(account.get("equity")) - _to_float(
            account.get("last_equity")
        )

        open_order_symbols = {
            self._alpaca_to_yahoo(str(item.get("symbol", "")))
            for item in open_orders
            if isinstance(item, dict)
        }
        open_order_symbols.discard("")

        refreshed: dict[str, Position] = {}
        for raw in broker_positions:
            if not isinstance(raw, dict):
                continue
            alpaca_symbol = str(raw.get("symbol", "")).upper()
            yahoo_symbol = self._alpaca_to_yahoo(alpaca_symbol)
            if not yahoo_symbol:
                continue
            meta = self.position_metadata.get(yahoo_symbol, {})
            side = str(raw.get("side", "long")).lower()
            quantity = abs(_to_float(raw.get("qty")))
            entry_price = _to_float(raw.get("avg_entry_price"))
            market_value = abs(_to_float(raw.get("market_value")))
            refreshed[yahoo_symbol] = Position(
                symbol=yahoo_symbol,
                side=side,
                quantity=quantity,
                entry_price=entry_price,
                stop_loss=_to_float(meta.get("stop_loss")),
                take_profit=_to_float(meta.get("take_profit")),
                reserved_cash=market_value,
                strategy=str(meta.get("strategy", "external_alpaca")),
                reason=str(meta.get("reason", "Position aus Alpaca synchronisiert.")),
                opened_at=str(meta.get("opened_at", raw.get("created_at", ""))),
            )
            self.pending_open.pop(yahoo_symbol, None)

        self.positions = refreshed

        now_epoch = time.time()
        for symbol, pending in list(self.pending_open.items()):
            if symbol in self.positions or symbol in open_order_symbols:
                continue
            if now_epoch - _to_float(pending.get("submitted_epoch")) > 1800:
                self._record(
                    "open_not_found",
                    symbol=symbol,
                    reason="Order nach 30 Minuten weder offen noch als Position gefunden.",
                )
                self.pending_open.pop(symbol, None)

        for symbol in list(self.pending_close):
            if symbol not in self.positions and symbol not in open_order_symbols:
                self.pending_close.pop(symbol, None)
                self.position_metadata.pop(symbol, None)
                self._record("close_completed", symbol=symbol)

        self.last_refresh = now
        self.last_error = ""
        self._save_state()

    def account_summary(self) -> dict[str, Any]:
        self.refresh()
        return {
            "broker": self.broker_name,
            "paper": True,
            "account": self.account_number_masked,
            "status": self.account_status,
            "currency": str(self.account.get("currency", "USD")),
            "cash": self.cash,
            "equity": self.equity(),
            "buying_power": _to_float(self.account.get("buying_power")),
            "day_pnl": self.realized_pnl,
            "orders_enabled": self.order_execution_enabled,
            "allow_shorts": self.allow_shorts,
        }

    def has_position(self, symbol: str) -> bool:
        return (
            symbol in self.positions
            or symbol in self.pending_open
            or symbol in self.pending_close
        )

    def equity(self, prices: Mapping[str, float] | None = None) -> float:
        del prices
        self.refresh()
        return _to_float(self.account.get("equity"))

    def get_asset(self, yahoo_symbol: str) -> dict[str, Any] | None:
        if yahoo_symbol in self.asset_cache:
            return self.asset_cache[yahoo_symbol]

        alpaca_symbol = self._yahoo_to_alpaca(yahoo_symbol)
        if not alpaca_symbol:
            self.asset_cache[yahoo_symbol] = None
            return None

        try:
            asset = self._request(
                "GET",
                f"/v2/assets/{quote(alpaca_symbol, safe='')}",
            )
        except AlpacaAPIError as exc:
            if "HTTP 404" not in str(exc):
                LOGGER.info("%s nicht nutzbar: %s", yahoo_symbol, exc)
            self.asset_cache[yahoo_symbol] = None
            return None

        if not isinstance(asset, dict):
            asset = None
        self.asset_cache[yahoo_symbol] = asset
        return asset

    def filter_tradable_symbols(self, symbols: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        selected: list[str] = []
        for symbol in symbols:
            asset = self.get_asset(symbol)
            if not asset:
                continue
            if str(asset.get("status", "")).lower() != "active":
                continue
            if not bool(asset.get("tradable", False)):
                continue
            if str(asset.get("class", "")).lower() != "us_equity":
                continue
            if self.require_fractionable and not bool(asset.get("fractionable", False)):
                continue
            selected.append(symbol)

        for symbol in self.positions:
            if symbol not in selected:
                selected.append(symbol)

        if not selected:
            raise AlpacaAPIError(
                "Keiner der Scanner-Märkte ist im Alpaca-Paperkonto als handelbare "
                "US-Aktie/ETF verfügbar. Starte den Scanner erneut."
            )
        return tuple(selected)

    def open_position(self, signal: Signal, quantity: float) -> Position | None:
        if not self.order_execution_enabled:
            LOGGER.info(
                "%s: Alpaca-Verbindung aktiv, aber Orders sind noch gesperrt.",
                signal.symbol,
            )
            return None
        if self.has_position(signal.symbol):
            return None
        if signal.direction is Direction.HOLD:
            return None
        if signal.stop_loss is None or signal.take_profit is None:
            return None
        if signal.direction is Direction.SHORT and not self.allow_shorts:
            LOGGER.info("%s: Short-Signal gesperrt.", signal.symbol)
            return None

        self.refresh(force=True)
        asset = self.get_asset(signal.symbol)
        if not asset or not bool(asset.get("tradable", False)):
            LOGGER.info("%s: bei Alpaca nicht handelbar.", signal.symbol)
            return None
        if self.require_fractionable and not bool(asset.get("fractionable", False)):
            LOGGER.info("%s: kein Bruchteilhandel, daher übersprungen.", signal.symbol)
            return None
        if signal.direction is Direction.SHORT and not bool(asset.get("shortable", False)):
            LOGGER.info("%s: bei Alpaca nicht shortbar.", signal.symbol)
            return None

        desired_notional = signal.entry_price * quantity
        available = max(self.cash * 0.98, 0.0)
        notional = min(desired_notional, self.max_order_notional, available)
        if not math.isfinite(notional) or notional < 1.0:
            LOGGER.info("%s: Orderwert unter 1 USD.", signal.symbol)
            return None

        alpaca_symbol = self._yahoo_to_alpaca(signal.symbol)
        if not alpaca_symbol:
            return None
        client_order_id = f"bot08-{uuid.uuid4().hex[:20]}"
        payload: dict[str, Any] = {
            "symbol": alpaca_symbol,
            "side": "buy" if signal.direction is Direction.LONG else "sell",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }

        if signal.direction is Direction.LONG and bool(asset.get("fractionable", False)):
            payload["notional"] = f"{notional:.2f}"
            submitted_quantity = notional / signal.entry_price
        else:
            submitted_quantity = min(quantity, notional / signal.entry_price)
            payload["qty"] = _format_qty(submitted_quantity)

        order = self._request("POST", "/v2/orders", payload=payload)
        order_id = str(order.get("id", "")) if isinstance(order, dict) else ""
        opened_at = _utc_iso()
        metadata = {
            "alpaca_symbol": alpaca_symbol,
            "side": signal.direction.value,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "strategy": signal.strategy,
            "reason": signal.reason,
            "opened_at": opened_at,
        }
        self.position_metadata[signal.symbol] = metadata
        self.pending_open[signal.symbol] = {
            **metadata,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "submitted_epoch": time.time(),
            "notional": notional,
        }
        self._record(
            "order_submitted",
            symbol=signal.symbol,
            side=signal.direction.value,
            quantity=submitted_quantity,
            notional=notional,
            order_id=order_id,
            strategy=signal.strategy,
        )
        self._save_state()

        return Position(
            symbol=signal.symbol,
            side=signal.direction.value,
            quantity=float(submitted_quantity),
            entry_price=signal.entry_price,
            stop_loss=float(signal.stop_loss),
            take_profit=float(signal.take_profit),
            reserved_cash=float(notional),
            strategy=signal.strategy,
            reason=signal.reason,
            opened_at=opened_at,
        )

    def market_clock(self) -> dict[str, Any]:
        data = self._request("GET", "/v2/clock")
        return data if isinstance(data, dict) else {}

    def raw_positions(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v2/positions")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def raw_orders(
        self,
        *,
        status: str = "all",
        limit: int = 50,
        nested: bool = True,
    ) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/v2/orders",
            params={
                "status": status,
                "limit": max(1, min(int(limit), 500)),
                "direction": "desc",
                "nested": str(bool(nested)).lower(),
            },
        )
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def open_order_symbols(self) -> set[str]:
        symbols = {
            self._alpaca_to_yahoo(str(item.get("symbol", "")))
            for item in self.raw_orders(status="open", limit=500, nested=True)
        }
        symbols.discard("")
        return symbols

    def open_bracket_position(
        self,
        signal: Signal,
        quantity: float,
        *,
        client_order_id: str,
    ) -> Position | None:
        """Submit a whole-share Alpaca Paper bracket order.

        GitHub Actions is not a permanent process, so the stop-loss and
        take-profit must live at Alpaca rather than inside a sleeping runner.
        """
        if not self.order_execution_enabled:
            LOGGER.info("%s: GitHub-Paperorders sind gesperrt.", signal.symbol)
            return None
        if self.has_position(signal.symbol):
            return None
        if signal.direction is not Direction.LONG:
            return None
        if signal.stop_loss is None or signal.take_profit is None:
            return None

        self.refresh(force=True)
        asset = self.get_asset(signal.symbol)
        if not asset or not bool(asset.get("tradable", False)):
            return None

        price = float(signal.entry_price)
        max_quantity = min(
            float(quantity),
            self.max_order_notional / max(price, 1e-12),
            max(self.cash * 0.98, 0.0) / max(price, 1e-12),
        )
        whole_quantity = int(math.floor(max_quantity))
        if whole_quantity < 1:
            LOGGER.info(
                "%s: Für den sicheren Cloud-Bracket-Trade ist eine ganze Aktie zu teuer.",
                signal.symbol,
            )
            return None

        take_profit = round(float(signal.take_profit), 2)
        stop_loss = round(float(signal.stop_loss), 2)
        if not (stop_loss > 0 and take_profit > price and stop_loss < price):
            LOGGER.info("%s: ungültige Bracket-Preise.", signal.symbol)
            return None

        alpaca_symbol = self._yahoo_to_alpaca(signal.symbol)
        if not alpaca_symbol:
            return None
        payload: dict[str, Any] = {
            "symbol": alpaca_symbol,
            "qty": str(whole_quantity),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_loss:.2f}"},
            "client_order_id": client_order_id[:48],
        }
        order = self._request("POST", "/v2/orders", payload=payload)
        order_id = str(order.get("id", "")) if isinstance(order, dict) else ""
        opened_at = _utc_iso()
        self.pending_open[signal.symbol] = {
            "order_id": order_id,
            "client_order_id": client_order_id[:48],
            "submitted_epoch": time.time(),
            "strategy": signal.strategy,
            "reason": signal.reason,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }
        self._record(
            "bracket_order_submitted",
            symbol=signal.symbol,
            side="long",
            quantity=whole_quantity,
            notional=whole_quantity * price,
            order_id=order_id,
            strategy=signal.strategy,
        )
        self._save_state()
        return Position(
            symbol=signal.symbol,
            side="long",
            quantity=float(whole_quantity),
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reserved_cash=float(whole_quantity * price),
            strategy=signal.strategy,
            reason=signal.reason,
            opened_at=opened_at,
        )

    def close_position(
        self,
        symbol: str,
        market_price: float,
        close_reason: str,
    ) -> float:
        del market_price
        if not self.order_execution_enabled:
            return 0.0
        if symbol in self.pending_close:
            return 0.0
        self.refresh(force=True)
        if symbol not in self.positions:
            return 0.0

        alpaca_symbol = self._yahoo_to_alpaca(symbol)
        if not alpaca_symbol:
            return 0.0
        order = self._request(
            "DELETE",
            f"/v2/positions/{quote(alpaca_symbol, safe='')}",
        )
        order_id = str(order.get("id", "")) if isinstance(order, dict) else ""
        self.pending_close[symbol] = {
            "order_id": order_id,
            "reason": close_reason,
            "submitted_epoch": time.time(),
        }
        self._record(
            "close_submitted",
            symbol=symbol,
            reason=close_reason,
            order_id=order_id,
        )
        self._save_state()
        return 0.0

    def update_positions(
        self,
        prices: Mapping[str, float],
    ) -> list[tuple[str, float, str]]:
        self.refresh()
        closed: list[tuple[str, float, str]] = []
        for symbol, position in list(self.positions.items()):
            if symbol in self.pending_close or symbol not in prices:
                continue
            if position.stop_loss <= 0 or position.take_profit <= 0:
                continue
            price = float(prices[symbol])
            if position.side == Direction.LONG.value:
                reason = (
                    "stop_loss" if price <= position.stop_loss
                    else "take_profit" if price >= position.take_profit
                    else ""
                )
            else:
                reason = (
                    "stop_loss" if price >= position.stop_loss
                    else "take_profit" if price <= position.take_profit
                    else ""
                )
            if not reason:
                continue
            self.close_position(symbol, price, reason)
            closed.append((symbol, 0.0, f"{reason}_order_submitted"))
        return closed

    def reset(self) -> None:
        raise RuntimeError(
            "Ein Alpaca-Konto wird nicht durch lokale Dateien zurückgesetzt. "
            "Positionen und Orders zuerst im Alpaca-Paper-Dashboard schließen."
        )

    def _record(self, event: str, **values: Any) -> None:
        self.trade_history.append(
            {"event": event, "time": _utc_iso(), **values}
        )
        self.trade_history = self.trade_history[-1000:]

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.position_metadata = dict(payload.get("position_metadata", {}))
            self.pending_open = dict(payload.get("pending_open", {}))
            self.pending_close = dict(payload.get("pending_close", {}))
            self.trade_history = list(payload.get("trade_history", []))
        except Exception:
            LOGGER.exception("Lokaler Alpaca-Metadaten-State konnte nicht geladen werden.")

    def _save_state(self) -> None:
        payload = {
            "paper_endpoint": PAPER_BASE_URL,
            "position_metadata": self.position_metadata,
            "pending_open": self.pending_open,
            "pending_close": self.pending_close,
            "trade_history": self.trade_history,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temp.replace(self.state_path)

    @staticmethod
    def _yahoo_to_alpaca(symbol: str) -> str | None:
        symbol = str(symbol).strip().upper()
        if not symbol or "=" in symbol or symbol.startswith("^"):
            return None
        if symbol.endswith("-EUR") or symbol.endswith("-USD"):
            return None
        if symbol.endswith(NON_US_SUFFIXES):
            return None
        if symbol in {"BRK-B", "BRK-A"}:
            return symbol.replace("-", ".")
        return symbol

    @staticmethod
    def _alpaca_to_yahoo(symbol: str) -> str:
        symbol = str(symbol).strip().upper()
        if symbol in {"BRK.A", "BRK.B"}:
            return symbol.replace(".", "-")
        return symbol


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_qty(value: float) -> str:
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return text or "0"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
