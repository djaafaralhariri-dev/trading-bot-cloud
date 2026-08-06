from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd
import yfinance as yf


LOGGER = logging.getLogger(__name__)
REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


class MarketDataError(RuntimeError):
    pass


class YahooMarketData:
    def fetch(
        self,
        symbol: str,
        *,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        try:
            frame = yf.Ticker(symbol).history(
                period=period,
                interval=interval,
                auto_adjust=False,
                actions=False,
            )
        except Exception as exc:
            raise MarketDataError(
                f"{symbol}: Datenabruf fehlgeschlagen: {exc}"
            ) from exc

        if frame is None or frame.empty:
            raise MarketDataError(f"{symbol}: keine Kursdaten erhalten.")

        frame = frame.rename(
            columns={column: column.lower() for column in frame.columns}
        )
        missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            raise MarketDataError(
                f"{symbol}: Spalten fehlen: {', '.join(missing)}"
            )

        result = frame.loc[:, REQUIRED_COLUMNS].copy()
        result = result.apply(pd.to_numeric, errors="coerce")
        result = result.replace([float("inf"), float("-inf")], pd.NA).dropna()

        if len(result) < 60:
            raise MarketDataError(
                f"{symbol}: nur {len(result)} gültige Zeilen; mindestens 60 benötigt."
            )

        result.index = pd.to_datetime(result.index)
        if getattr(result.index, "tz", None) is not None:
            result.index = result.index.tz_convert(None)

        result = result[~result.index.duplicated(keep="last")].sort_index()

        invalid = (
            (result["high"] < result["low"])
            | (result["close"] <= 0)
            | (result["open"] <= 0)
            | (result["volume"] < 0)
        )
        if invalid.any():
            LOGGER.warning(
                "%s: %s ungültige Zeilen entfernt.",
                symbol,
                int(invalid.sum()),
            )
            result = result.loc[~invalid]

        if len(result) < 60:
            raise MarketDataError(
                f"{symbol}: nach Bereinigung zu wenige Daten."
            )

        return result

    def fetch_many(
        self,
        symbols: Iterable[str],
        *,
        period: str,
        interval: str,
    ) -> dict[str, pd.DataFrame]:
        output: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                output[symbol] = self.fetch(
                    symbol,
                    period=period,
                    interval=interval,
                )
            except MarketDataError as exc:
                LOGGER.error("%s", exc)
        return output
