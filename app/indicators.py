from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / window,
        adjust=False,
        min_periods=window,
    ).mean()
    avg_loss = loss.ewm(
        alpha=1 / window,
        adjust=False,
        min_periods=window,
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50.0)


def atr(frame: pd.DataFrame, window: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    )
    true_range = ranges.max(axis=1)
    return true_range.rolling(window=window, min_periods=window).mean()


def enrich(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    data["sma_20"] = sma(data["close"], 20)
    data["sma_50"] = sma(data["close"], 50)
    data["ema_12"] = ema(data["close"], 12)
    data["ema_26"] = ema(data["close"], 26)
    data["macd"] = data["ema_12"] - data["ema_26"]
    data["macd_signal"] = ema(data["macd"], 9)
    data["rsi_14"] = rsi(data["close"], 14)
    data["atr_14"] = atr(data, 14)
    data["return_20"] = data["close"].pct_change(20)
    data["volume_sma_20"] = sma(data["volume"], 20)
    data["high_20"] = data["high"].shift(1).rolling(20).max()
    data["low_20"] = data["low"].shift(1).rolling(20).min()

    rolling_mean = sma(data["close"], 20)
    rolling_std = data["close"].rolling(20).std()
    data["zscore_20"] = (
        (data["close"] - rolling_mean)
        / rolling_std.replace(0, np.nan)
    )

    return data.dropna().copy()
