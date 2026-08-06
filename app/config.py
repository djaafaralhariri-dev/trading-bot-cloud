from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

if not ENV_FILE.exists() and (BASE_DIR / ".env.example").exists():
    ENV_FILE.write_text(
        (BASE_DIR / ".env.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

load_dotenv(ENV_FILE)


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} muss eine Zahl sein.") from exc


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} muss eine ganze Zahl sein.") from exc


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "ja", "on"}:
        return True
    if raw in {"0", "false", "no", "nein", "off"}:
        return False
    raise ValueError(f"{name} muss true oder false sein.")


def env_csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def env_symbols() -> tuple[str, ...]:
    raw = os.getenv(
        "SYMBOLS",
        "BTC-EUR,ETH-EUR,AAPL,MSFT,SPY,GLD,EURUSD=X",
    )
    symbols = tuple(item.strip().upper() for item in raw.split(",") if item.strip())
    if not symbols:
        raise ValueError("SYMBOLS darf nicht leer sein.")
    return symbols


@dataclass(frozen=True, slots=True)
class Settings:
    starting_cash: float = env_float("STARTING_CASH", 250.0)
    risk_per_trade: float = env_float("RISK_PER_TRADE", 0.005)
    max_position_fraction: float = env_float("MAX_POSITION_FRACTION", 0.35)
    max_open_positions: int = env_int("MAX_OPEN_POSITIONS", 5)
    fee_rate: float = env_float("FEE_RATE", 0.001)
    slippage_rate: float = env_float("SLIPPAGE_RATE", 0.0005)
    data_period: str = os.getenv("DATA_PERIOD", "1y")
    data_interval: str = os.getenv("DATA_INTERVAL", "1d")
    loop_seconds: int = env_int("LOOP_SECONDS", 900)
    symbols: tuple[str, ...] = env_symbols()
    min_signal_confidence: float = env_float("MIN_SIGNAL_CONFIDENCE", 0.58)
    min_strategy_agreement: int = env_int("MIN_STRATEGY_AGREEMENT", 2)
    live_history_period: str = os.getenv("LIVE_HISTORY_PERIOD", "5d")
    live_history_interval: str = os.getenv("LIVE_HISTORY_INTERVAL", "1m")
    live_poll_seconds: int = env_int("LIVE_POLL_SECONDS", 60)
    live_status_write_seconds: int = env_int("LIVE_STATUS_WRITE_SECONDS", 2)
    live_max_bars: int = env_int("LIVE_MAX_BARS", 5000)
    live_max_reconnect_seconds: int = env_int("LIVE_MAX_RECONNECT_SECONDS", 60)

    scanner_enabled: bool = env_bool("SCANNER_ENABLED", True)
    scanner_top_n: int = env_int("SCANNER_TOP_N", 25)
    scanner_period: str = os.getenv("SCANNER_PERIOD", "3mo")
    scanner_interval: str = os.getenv("SCANNER_INTERVAL", "1d")
    scanner_batch_size: int = env_int("SCANNER_BATCH_SIZE", 40)
    scanner_screener_count: int = env_int("SCANNER_SCREENER_COUNT", 40)
    scanner_max_candidates: int = env_int("SCANNER_MAX_CANDIDATES", 300)
    scanner_min_price: float = env_float("SCANNER_MIN_PRICE", 1.0)
    scanner_min_dollar_volume: float = env_float("SCANNER_MIN_DOLLAR_VOLUME", 5_000_000)
    scanner_stock_slots: int = env_int("SCANNER_STOCK_SLOTS", 14)
    scanner_etf_slots: int = env_int("SCANNER_ETF_SLOTS", 5)
    scanner_crypto_slots: int = env_int("SCANNER_CRYPTO_SLOTS", 4)
    scanner_forex_slots: int = env_int("SCANNER_FOREX_SLOTS", 2)
    scanner_dynamic_screeners: tuple[str, ...] = env_csv(
        "SCANNER_DYNAMIC_SCREENERS",
        "most_actives,day_gainers,day_losers,growth_technology_stocks",
    )

    # v0.7 Alpaca Paper API. The broker adapter itself is hard-coded to the
    # paper endpoint and has no live switch.
    broker_mode: str = os.getenv("BROKER_MODE", "local").strip().lower()
    alpaca_api_key: str = os.getenv("ALPACA_API_KEY", "").strip()
    alpaca_secret_key: str = os.getenv("ALPACA_SECRET_KEY", "").strip()
    alpaca_paper: bool = env_bool("ALPACA_PAPER", True)
    alpaca_order_execution: bool = env_bool("ALPACA_ORDER_EXECUTION", False)
    alpaca_allow_shorts: bool = env_bool("ALPACA_ALLOW_SHORTS", False)
    alpaca_max_order_notional: float = env_float("ALPACA_MAX_ORDER_NOTIONAL", 75.0)
    alpaca_refresh_seconds: int = env_int("ALPACA_REFRESH_SECONDS", 10)
    alpaca_data_feed: str = os.getenv("ALPACA_DATA_FEED", "iex").strip().lower()
    alpaca_require_fractionable: bool = env_bool(
        "ALPACA_REQUIRE_FRACTIONABLE", True
    )

    @property
    def state_path(self) -> Path:
        return BASE_DIR / "paper_state.json"

    @property
    def log_path(self) -> Path:
        return BASE_DIR / "logs" / "trading_bot.log"

    @property
    def live_state_path(self) -> Path:
        return BASE_DIR / "live_paper_state.json"

    @property
    def live_status_path(self) -> Path:
        return BASE_DIR / "live_status.json"

    @property
    def live_data_dir(self) -> Path:
        return BASE_DIR / "live_data"

    @property
    def live_log_path(self) -> Path:
        return BASE_DIR / "logs" / "live_paper.log"

    @property
    def scanner_output_dir(self) -> Path:
        return BASE_DIR / "scanner_output"

    @property
    def scanner_ranked_path(self) -> Path:
        return self.scanner_output_dir / "ranked_markets.csv"

    @property
    def scanner_selected_path(self) -> Path:
        return self.scanner_output_dir / "selected_markets.csv"

    @property
    def scanner_summary_path(self) -> Path:
        return self.scanner_output_dir / "summary.json"

    @property
    def active_symbols_path(self) -> Path:
        return self.scanner_output_dir / "active_symbols.json"

    @property
    def scanner_log_path(self) -> Path:
        return BASE_DIR / "logs" / "market_scanner.log"

    @property
    def alpaca_state_path(self) -> Path:
        return BASE_DIR / "alpaca_paper_state.json"


settings = Settings()
