from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd
import requests


DATA_BASE_URL = "https://data.alpaca.markets"
_REQUIRED = ("open", "high", "low", "close", "volume")


class AlpacaMarketDataError(RuntimeError):
    pass


class AlpacaHistoricalBars:
    """Small REST client for Alpaca's historical US-equity minute bars."""

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        feed: str = "iex",
        session: requests.Session | None = None,
    ) -> None:
        if not api_key.strip() or not secret_key.strip():
            raise ValueError("Alpaca-Paper-Keys fehlen.")
        self.feed = feed.strip().lower() or "iex"
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": api_key.strip(),
                "APCA-API-SECRET-KEY": secret_key.strip(),
                "Accept": "application/json",
                "User-Agent": "ai-trading-bot-github-cloud/1.0",
            }
        )

    def fetch_recent(
        self,
        symbols: Iterable[str],
        *,
        lookback_days: int = 4,
        timeframe: str = "1Min",
        max_bars_per_symbol: int = 600,
    ) -> dict[str, pd.DataFrame]:
        cleaned = tuple(dict.fromkeys(str(item).strip().upper() for item in symbols if str(item).strip()))
        if not cleaned:
            return {}

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=max(2, int(lookback_days)))
        params: dict[str, Any] = {
            "symbols": ",".join(cleaned),
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": now.isoformat(),
            "limit": 10000,
            "adjustment": "all",
            "feed": self.feed,
            "sort": "asc",
        }

        collected: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in cleaned}
        page_token = ""
        pages = 0
        while True:
            if page_token:
                params["page_token"] = page_token
            else:
                params.pop("page_token", None)

            try:
                response = self.session.get(
                    f"{DATA_BASE_URL}/v2/stocks/bars",
                    params=params,
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise AlpacaMarketDataError(f"Alpaca-Marktdaten nicht erreichbar: {exc}") from exc

            try:
                payload = response.json()
            except ValueError as exc:
                raise AlpacaMarketDataError("Alpaca lieferte keine gültigen JSON-Marktdaten.") from exc

            if not response.ok:
                message = payload.get("message", "") if isinstance(payload, dict) else ""
                raise AlpacaMarketDataError(
                    f"Alpaca-Marktdaten HTTP {response.status_code}: {message or response.text[:300]}"
                )

            bars = payload.get("bars", {}) if isinstance(payload, dict) else {}
            if isinstance(bars, dict):
                for symbol, rows in bars.items():
                    key = str(symbol).upper()
                    if key not in collected or not isinstance(rows, list):
                        continue
                    collected[key].extend(item for item in rows if isinstance(item, dict))

            page_token = str(payload.get("next_page_token") or "") if isinstance(payload, dict) else ""
            pages += 1
            if not page_token or pages >= 10:
                break

        output: dict[str, pd.DataFrame] = {}
        for symbol, rows in collected.items():
            frame = _bars_to_frame(rows)
            if frame.empty:
                continue
            output[symbol] = frame.tail(max(60, int(max_bars_per_symbol)))
        return output


def _bars_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=_REQUIRED)

    frame = pd.DataFrame(rows)
    rename = {
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "t": "timestamp",
    }
    frame = frame.rename(columns=rename)
    if not all(column in frame.columns for column in (*_REQUIRED, "timestamp")):
        return pd.DataFrame(columns=_REQUIRED)

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    for column in _REQUIRED:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=list(_REQUIRED))
    frame = frame.loc[
        (frame["open"] > 0)
        & (frame["high"] > 0)
        & (frame["low"] > 0)
        & (frame["close"] > 0)
        & (frame["high"] >= frame["low"])
        & (frame["volume"] >= 0)
    ]
    frame = frame.set_index("timestamp").loc[:, list(_REQUIRED)]
    frame.index = frame.index.tz_convert(None)
    return frame.loc[~frame.index.duplicated(keep="last")].sort_index()
