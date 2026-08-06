from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd
import yfinance as yf

from app.config import settings
from app.indicators import enrich
from app.strategies import Direction, StrategyEngine


LOGGER = logging.getLogger("scanner")
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.^=\-]{1,24}$")

# The stock list is only a fallback. The normal path gets fresh candidates from
# Yahoo's dynamic screeners, then merges the cross-asset lists below.
FALLBACK_STOCKS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    "BRK-B", "JPM", "V", "MA", "WMT", "COST", "LLY", "XOM", "UNH",
    "JNJ", "PG", "HD", "ABBV", "KO", "PEP", "CRM", "NFLX", "AMD",
    "ORCL", "BAC", "CSCO", "QCOM", "TMO", "MCD", "CAT", "GE", "IBM",
    "AMAT", "MU", "PLTR", "UBER", "SHOP", "TSM", "ASML", "SAP", "NVO",
    "TM", "SONY", "DIS", "INTC", "NKE", "PYPL",
)


GLOBAL_STOCKS: tuple[str, ...] = (
    # Germany and continental Europe
    "SAP.DE", "SIE.DE", "ALV.DE", "DTE.DE", "AIR.PA", "MBG.DE",
    "BMW.DE", "VOW3.DE", "BAS.DE", "BAYN.DE", "RWE.DE", "DBK.DE",
    "ADS.DE", "IFX.DE", "DHL.DE", "MUV2.DE", "HNR1.DE", "ENR.DE",
    "VNA.DE", "BEI.DE", "ASML.AS", "MC.PA", "OR.PA", "TTE.PA",
    "SAN.PA", "BNP.PA", "SU.PA", "RMS.PA", "NESN.SW", "NOVN.SW",
    "ROG.SW", "SHEL.L", "AZN.L", "HSBA.L", "ULVR.L", "BP.L",
    # Asia-Pacific
    "7203.T", "6758.T", "9984.T", "005930.KS", "0700.HK", "9988.HK",
    "2330.TW", "BHP.AX",
)

ETF_SYMBOLS: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI",
    "XLY", "XLP", "XLU", "XLB", "XLRE", "SMH", "SOXX", "TLT", "HYG",
    "LQD", "GLD", "SLV", "USO", "DBA", "UUP", "EEM", "EFA", "VGK",
    "EWJ", "INDA", "VNQ",
)

CRYPTO_SYMBOLS: tuple[str, ...] = (
    "BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "BNB-EUR", "ADA-EUR",
    "DOGE-EUR", "AVAX-EUR", "LINK-EUR", "DOT-EUR", "LTC-EUR", "BCH-EUR",
    "XLM-EUR", "UNI-EUR", "ATOM-EUR", "ETC-EUR", "FIL-EUR", "APT-EUR",
)

FOREX_SYMBOLS: tuple[str, ...] = (
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "USDCAD=X", "NZDUSD=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X",
)

ASSET_LABELS = {
    "stock": "Aktie",
    "etf": "ETF",
    "crypto": "Krypto",
    "forex": "Forex",
}


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    asset_type: str
    source: str


@dataclass(frozen=True, slots=True)
class ScanRecord:
    symbol: str
    asset_type: str
    source: str
    score: float
    signal: str
    confidence: float
    strategy: str
    price: float
    return_5_pct: float
    return_20_pct: float
    atr_pct: float
    rsi_14: float
    avg_dollar_volume: float
    volume_ratio: float
    last_bar: str
    reason: str


class MarketScanner:
    def __init__(
        self,
        *,
        screen_func: Callable[..., Any] | None = None,
        download_func: Callable[..., Any] | None = None,
    ) -> None:
        self.screen_func = screen_func or yf.screen
        self.download_func = download_func or yf.download
        self.errors: list[str] = []
        self.engine = StrategyEngine(
            settings.min_signal_confidence,
            settings.min_strategy_agreement,
        )

    def collect_candidates(self) -> list[Candidate]:
        candidates: dict[str, Candidate] = {}

        def add(symbol: str, asset_type: str, source: str) -> None:
            cleaned = str(symbol).strip().upper()
            if not _valid_symbol(cleaned):
                return
            existing = candidates.get(cleaned)
            if existing is None or existing.source.startswith("fallback"):
                candidates[cleaned] = Candidate(cleaned, asset_type, source)

        for symbol in FALLBACK_STOCKS:
            add(symbol, "stock", "fallback_stocks")
        for symbol in GLOBAL_STOCKS:
            add(symbol, "stock", "global_stocks")
        for symbol in ETF_SYMBOLS:
            add(symbol, "etf", "core_etfs")
        for symbol in CRYPTO_SYMBOLS:
            add(symbol, "crypto", "major_crypto")
        for symbol in FOREX_SYMBOLS:
            add(symbol, "forex", "major_forex")
        for symbol in settings.symbols:
            add(symbol, classify_symbol(symbol), "user_symbols")

        for screener_name in settings.scanner_dynamic_screeners:
            try:
                response = self.screen_func(
                    screener_name,
                    count=settings.scanner_screener_count,
                )
                quotes = _find_quotes(response)
                if not quotes:
                    self.errors.append(f"{screener_name}: keine Treffer")
                    continue
                for quote in quotes:
                    symbol = quote.get("symbol") or quote.get("ticker")
                    quote_type = str(
                        quote.get("quoteType")
                        or quote.get("quote_type")
                        or "EQUITY"
                    ).upper()
                    if quote_type == "ETF":
                        asset_type = "etf"
                    elif quote_type in {"EQUITY", "STOCK"}:
                        asset_type = "stock"
                    else:
                        continue
                    add(str(symbol or ""), asset_type, f"screener:{screener_name}")
            except Exception as exc:
                message = f"{screener_name}: {type(exc).__name__}: {exc}"
                self.errors.append(message)
                LOGGER.warning("Screener fehlgeschlagen: %s", message)

        ordered = sorted(
            candidates.values(),
            key=lambda item: (
                0 if item.source.startswith("screener:") else 1,
                item.asset_type,
                item.symbol,
            ),
        )
        return ordered[: settings.scanner_max_candidates]

    def download_history(
        self,
        candidates: Sequence[Candidate],
    ) -> dict[str, pd.DataFrame]:
        output: dict[str, pd.DataFrame] = {}
        symbol_to_candidate = {item.symbol: item for item in candidates}
        symbols = list(symbol_to_candidate)
        batch_size = max(1, settings.scanner_batch_size)

        for start in range(0, len(symbols), batch_size):
            batch = symbols[start : start + batch_size]
            frames = self._download_batch_with_recovery(batch)
            output.update(frames)
            LOGGER.info(
                "Historie geladen: %s/%s Kandidaten",
                min(start + len(batch), len(symbols)),
                len(symbols),
            )
        return output

    def _download_batch_with_recovery(
        self,
        symbols: Sequence[str],
    ) -> dict[str, pd.DataFrame]:
        if not symbols:
            return {}
        try:
            raw = self.download_func(
                tickers=list(symbols),
                period=settings.scanner_period,
                interval=settings.scanner_interval,
                group_by="ticker",
                auto_adjust=True,
                repair=True,
                progress=False,
                threads=True,
                timeout=30,
            )
            frames = _split_download(raw, symbols)
        except Exception as exc:
            frames = {}
            if len(symbols) == 1:
                self.errors.append(f"{symbols[0]}: Downloadfehler: {exc}")
                return {}

        missing = [symbol for symbol in symbols if symbol not in frames]
        if missing and len(symbols) > 1:
            midpoint = max(1, len(missing) // 2)
            for part in (missing[:midpoint], missing[midpoint:]):
                if part:
                    frames.update(self._download_batch_with_recovery(part))
        elif missing:
            self.errors.append(f"{missing[0]}: keine brauchbaren Kursdaten")
        return frames

    def analyse(
        self,
        candidates: Sequence[Candidate],
        histories: dict[str, pd.DataFrame],
    ) -> list[ScanRecord]:
        records: list[ScanRecord] = []
        for candidate in candidates:
            frame = histories.get(candidate.symbol)
            if frame is None or frame.empty:
                continue
            try:
                record = self.score_candidate(candidate, frame)
            except Exception as exc:
                self.errors.append(
                    f"{candidate.symbol}: Analysefehler: {type(exc).__name__}: {exc}"
                )
                continue
            if record is not None:
                records.append(record)
        return sorted(records, key=lambda item: item.score, reverse=True)

    def score_candidate(
        self,
        candidate: Candidate,
        frame: pd.DataFrame,
    ) -> ScanRecord | None:
        clean = _normalise_ohlcv(frame)
        if len(clean) < 60:
            return None

        prepared = enrich(clean)
        if prepared.empty:
            return None

        row = prepared.iloc[-1]
        price = float(row["close"])
        if not math.isfinite(price) or price <= 0:
            return None

        asset_type = candidate.asset_type
        if asset_type in {"stock", "etf"} and price < settings.scanner_min_price:
            return None

        avg_volume = float(clean["volume"].tail(20).mean())
        avg_dollar_volume = max(avg_volume, 0.0) * price
        liquidity_floor = settings.scanner_min_dollar_volume
        if asset_type == "etf":
            liquidity_floor *= 0.5
        if asset_type in {"stock", "etf"} and avg_dollar_volume < liquidity_floor:
            return None

        returns = clean["close"].pct_change()
        return_5 = float(clean["close"].pct_change(5).iloc[-1])
        return_20 = float(row["return_20"])
        atr_pct = float(row["atr_14"] / price)
        rsi_value = float(row["rsi_14"])
        volume_sma = max(float(row["volume_sma_20"]), 1e-12)
        volume_ratio = float(row["volume"] / volume_sma)
        annual_volatility = float(returns.tail(20).std(ddof=0) * math.sqrt(252))

        if not all(
            math.isfinite(value)
            for value in (
                return_5,
                return_20,
                atr_pct,
                rsi_value,
                volume_ratio,
                annual_volatility,
            )
        ):
            return None
        if atr_pct <= 0 or atr_pct > 0.25:
            return None

        signal_result = self.engine.analyze(candidate.symbol, prepared)

        trend_gap = abs(float(row["sma_20"] / row["sma_50"] - 1))
        trend_points = _clamp(trend_gap / 0.08) * 20
        momentum_points = _clamp(abs(return_20) / 0.15) * 15
        rsi_points = _clamp(abs(rsi_value - 50) / 25) * 7
        volatility_points = _volatility_score(atr_pct, annual_volatility) * 12
        liquidity_points = _liquidity_score(asset_type, avg_dollar_volume) * 13
        activity_points = _clamp((volume_ratio - 0.75) / 1.25) * 5

        nearest_breakout = min(
            abs(price - float(row["high_20"])),
            abs(price - float(row["low_20"])),
        )
        breakout_points = (1 - _clamp(nearest_breakout / max(float(row["atr_14"]) * 2, 1e-12))) * 6

        signal_points = 0.0
        if signal_result.direction is not Direction.HOLD:
            signal_points = signal_result.confidence * 22

        score = (
            5
            + trend_points
            + momentum_points
            + rsi_points
            + volatility_points
            + liquidity_points
            + activity_points
            + breakout_points
            + signal_points
        )
        if atr_pct > 0.12:
            score -= 12
        score = round(_clamp(score / 100) * 100, 2)

        if signal_result.direction is Direction.HOLD:
            reason = (
                "Noch kein Handelssignal. Der Markt ist wegen Liquidität, "
                "Bewegung und technischer Nähe auf der Beobachtungsliste."
            )
        else:
            reason = signal_result.reason

        last_bar = pd.Timestamp(clean.index[-1]).isoformat()
        return ScanRecord(
            symbol=candidate.symbol,
            asset_type=asset_type,
            source=candidate.source,
            score=score,
            signal=signal_result.direction.value,
            confidence=round(float(signal_result.confidence), 4),
            strategy=signal_result.strategy,
            price=round(price, 8),
            return_5_pct=round(return_5 * 100, 4),
            return_20_pct=round(return_20 * 100, 4),
            atr_pct=round(atr_pct * 100, 4),
            rsi_14=round(rsi_value, 2),
            avg_dollar_volume=round(avg_dollar_volume, 2),
            volume_ratio=round(volume_ratio, 4),
            last_bar=last_bar,
            reason=reason,
        )


def classify_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol in ETF_SYMBOLS:
        return "etf"
    if symbol.endswith("=X"):
        return "forex"
    if symbol.endswith("-EUR") or symbol.endswith("-USD"):
        return "crypto"
    return "stock"


def select_diverse(
    records: Sequence[ScanRecord],
    top_n: int,
) -> list[ScanRecord]:
    top_n = max(1, top_n)
    caps = {
        "stock": max(0, settings.scanner_stock_slots),
        "etf": max(0, settings.scanner_etf_slots),
        "crypto": max(0, settings.scanner_crypto_slots),
        "forex": max(0, settings.scanner_forex_slots),
    }
    selected: list[ScanRecord] = []
    used: set[str] = set()

    counts = {asset_type: 0 for asset_type in caps}
    for asset_type in ("stock", "etf", "crypto", "forex"):
        limit = caps[asset_type]
        for record in records:
            if len(selected) >= top_n or counts[asset_type] >= limit:
                break
            if record.asset_type == asset_type and record.symbol not in used:
                selected.append(record)
                used.add(record.symbol)
                counts[asset_type] += 1

    for record in records:
        if len(selected) >= top_n:
            break
        if record.symbol not in used:
            selected.append(record)
            used.add(record.symbol)

    return selected[:top_n]


def load_open_position_symbols() -> tuple[str, ...]:
    path = settings.live_state_path
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        positions = payload.get("positions", {}) or {}
        return tuple(
            symbol.upper()
            for symbol in positions
            if _valid_symbol(str(symbol).upper())
        )
    except Exception:
        return ()


def load_active_symbols() -> tuple[str, ...]:
    symbols: list[str] = []
    if settings.active_symbols_path.exists():
        try:
            payload = json.loads(
                settings.active_symbols_path.read_text(encoding="utf-8")
            )
            symbols.extend(str(item).upper() for item in payload.get("symbols", []))
        except Exception:
            LOGGER.exception("Aktive Scanner-Symbole konnten nicht geladen werden.")

    if not symbols:
        symbols.extend(settings.symbols)
    symbols.extend(load_open_position_symbols())
    return tuple(dict.fromkeys(item for item in symbols if _valid_symbol(item)))


def run_scan(top_n: int | None = None) -> dict[str, Any]:
    configure_logging()
    started_at = _utc_iso()
    scanner = MarketScanner()
    candidates = scanner.collect_candidates()
    histories = scanner.download_history(candidates)
    records = scanner.analyse(candidates, histories)

    top_n = top_n or settings.scanner_top_n
    selected = select_diverse(records, top_n) if records else []
    open_symbols = load_open_position_symbols()

    active_symbols = [record.symbol for record in selected]
    for symbol in open_symbols:
        if symbol not in active_symbols:
            active_symbols.append(symbol)

    state = "ok"
    if not active_symbols:
        state = "fallback"
        previous = load_active_symbols()
        active_symbols = list(previous or settings.symbols)

    ranked_rows = []
    selected_set = {record.symbol for record in selected}
    for rank, record in enumerate(records, start=1):
        row = asdict(record)
        row["rank"] = rank
        row["asset_label"] = ASSET_LABELS.get(record.asset_type, record.asset_type)
        row["selected"] = record.symbol in selected_set
        ranked_rows.append(row)

    selected_rows = [row for row in ranked_rows if row["selected"]]
    settings.scanner_output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_csv_write(settings.scanner_ranked_path, pd.DataFrame(ranked_rows))
    _atomic_csv_write(settings.scanner_selected_path, pd.DataFrame(selected_rows))

    active_payload = {
        "generated_at": _utc_iso(),
        "symbols": active_symbols,
        "selected_symbols": [record.symbol for record in selected],
        "open_position_symbols": list(open_symbols),
    }
    _atomic_json_write(settings.active_symbols_path, active_payload)

    summary = {
        "state": state,
        "started_at": started_at,
        "finished_at": _utc_iso(),
        "candidate_count": len(candidates),
        "downloaded_count": len(histories),
        "analysed_count": len(records),
        "selected_count": len(selected),
        "active_count": len(active_symbols),
        "top_n": top_n,
        "period": settings.scanner_period,
        "interval": settings.scanner_interval,
        "dynamic_screeners": list(settings.scanner_dynamic_screeners),
        "errors_count": len(scanner.errors),
        "errors": scanner.errors[:100],
        "symbols": active_symbols,
    }
    _atomic_json_write(settings.scanner_summary_path, summary)
    return summary


def _find_quotes(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        direct = response.get("quotes")
        if isinstance(direct, list):
            return [item for item in direct if isinstance(item, dict)]
        for value in response.values():
            found = _find_quotes(value)
            if found:
                return found
    elif isinstance(response, list):
        if response and all(isinstance(item, dict) for item in response):
            if any("symbol" in item or "ticker" in item for item in response):
                return response
        for item in response:
            found = _find_quotes(item)
            if found:
                return found
    return []


def _split_download(
    raw: Any,
    symbols: Sequence[str],
) -> dict[str, pd.DataFrame]:
    if raw is None or not isinstance(raw, pd.DataFrame) or raw.empty:
        return {}

    output: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        level0_map = {
            str(item).upper(): item for item in raw.columns.get_level_values(0)
        }
        level1_map = {
            str(item).upper(): item for item in raw.columns.get_level_values(1)
        }
        for symbol in symbols:
            try:
                if symbol.upper() in level0_map:
                    frame = raw.xs(
                        level0_map[symbol.upper()], axis=1, level=0, drop_level=True
                    )
                elif symbol.upper() in level1_map:
                    frame = raw.xs(
                        level1_map[symbol.upper()], axis=1, level=1, drop_level=True
                    )
                else:
                    continue
                clean = _normalise_ohlcv(frame)
                if not clean.empty:
                    output[symbol] = clean
            except Exception:
                continue
    elif len(symbols) == 1:
        clean = _normalise_ohlcv(raw)
        if not clean.empty:
            output[symbols[0]] = clean
    return output


def _normalise_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [str(column[0]) for column in data.columns]
    data.columns = [str(column).strip().lower().replace("_", " ") for column in data.columns]

    aliases = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    rename = {column: aliases[column] for column in data.columns if column in aliases}
    data = data.rename(columns=rename)
    required_prices = ["open", "high", "low", "close"]
    if not all(column in data.columns for column in required_prices):
        return pd.DataFrame()
    if "volume" not in data.columns:
        data["volume"] = 0.0

    data = data.loc[:, ["open", "high", "low", "close", "volume"]]
    for column in data.columns:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["volume"] = data["volume"].fillna(0.0)
    data = data.replace([np.inf, -np.inf], np.nan).dropna(subset=required_prices)
    data = data.loc[
        (data["open"] > 0)
        & (data["high"] > 0)
        & (data["low"] > 0)
        & (data["close"] > 0)
        & (data["high"] >= data["low"])
        & (data["volume"] >= 0)
    ]
    data.index = pd.to_datetime(data.index, errors="coerce")
    data = data.loc[~data.index.isna()]
    if getattr(data.index, "tz", None) is not None:
        data.index = data.index.tz_convert(None)
    return data.loc[~data.index.duplicated(keep="last")].sort_index()


def _volatility_score(atr_pct: float, annual_volatility: float) -> float:
    # Enough movement to be tradable, but no meme-coin furnace.
    atr_part = 1 - _clamp(abs(atr_pct - 0.035) / 0.08)
    annual_part = 1 - _clamp(abs(annual_volatility - 0.45) / 1.2)
    return _clamp(atr_part * 0.65 + annual_part * 0.35)


def _liquidity_score(asset_type: str, avg_dollar_volume: float) -> float:
    if asset_type == "forex":
        return 0.8
    if avg_dollar_volume <= 0:
        return 0.45 if asset_type == "crypto" else 0.0
    log_value = math.log10(max(avg_dollar_volume, 1))
    return _clamp((log_value - 6) / 4)


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def _valid_symbol(symbol: str) -> bool:
    return bool(SYMBOL_PATTERN.fullmatch(symbol))


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp.replace(path)


def _atomic_csv_write(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temp, index=False)
    temp.replace(path)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def configure_logging() -> None:
    settings.scanner_log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    logger = logging.getLogger("scanner")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(settings.scanner_log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Breiter Markt-Scanner")
    parser.add_argument("--top", type=int, default=settings.scanner_top_n)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print("\nMARKT-SCANNER")
    print("Breiter Scan, danach werden nur die besten Märkte live beobachtet.\n")
    summary = run_scan(top_n=args.top)
    print(
        f"Kandidaten: {summary['candidate_count']} | "
        f"analysiert: {summary['analysed_count']} | "
        f"aktiv: {summary['active_count']}"
    )
    print("Auswahl:", ", ".join(summary["symbols"]))
    if summary["errors_count"]:
        print(
            f"Hinweis: {summary['errors_count']} Kandidaten/Quellen wurden übersprungen. "
            "Das ist bei kostenlosen Marktdaten normal."
        )


if __name__ == "__main__":
    main()
