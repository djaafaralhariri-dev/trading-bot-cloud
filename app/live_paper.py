from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import signal
import sys
import time
from typing import Any

import pandas as pd
import yfinance as yf

from app.config import settings
from app.alpaca_broker import AlpacaPaperBroker
from app.alpaca_stream import (
    AlpacaMinuteBar,
    AlpacaStockStream,
    AlpacaTradeTick,
)
from app.indicators import enrich
from app.live_candles import MinuteCandleStore
from app.market_data import YahooMarketData
from app.market_scanner import load_active_symbols, run_scan
from app.paper_broker import PaperBroker
from app.risk import RiskManager
from app.strategies import Direction, StrategyEngine


class LivePaperRunner:
    def __init__(self, source: str) -> None:
        self.requested_source = source
        self.source = (
            f"alpaca_{settings.alpaca_data_feed}"
            if source == "alpaca"
            else source
        )
        candidate_symbols = load_active_symbols()
        self.running = True
        self.started_at = _utc_iso()
        self.last_status_write = 0.0
        self.latest_prices: dict[str, float] = {}
        self.last_processed_bar: dict[str, pd.Timestamp] = {}
        self.last_tick: dict[str, str] = {}
        self.last_bar: dict[str, str] = {}
        self.last_signal: dict[str, dict[str, Any]] = {}
        self.error = ""
        self.connected = False
        self.reconnects = 0
        self.active_stream: AlpacaStockStream | None = None

        if settings.broker_mode == "alpaca":
            if not settings.alpaca_paper:
                raise RuntimeError(
                    "ALPACA_PAPER muss true bleiben. Live-Trading ist in v0.8 gesperrt."
                )
            self.broker = AlpacaPaperBroker(
                api_key=settings.alpaca_api_key,
                secret_key=settings.alpaca_secret_key,
                state_path=settings.alpaca_state_path,
                order_execution_enabled=settings.alpaca_order_execution,
                allow_shorts=settings.alpaca_allow_shorts,
                max_order_notional=settings.alpaca_max_order_notional,
                refresh_seconds=settings.alpaca_refresh_seconds,
                require_fractionable=settings.alpaca_require_fractionable,
            )
            self.symbols = self.broker.filter_tradable_symbols(candidate_symbols)
        elif settings.broker_mode == "local":
            self.broker = PaperBroker(
                settings.starting_cash,
                settings.fee_rate,
                settings.slippage_rate,
                settings.live_state_path,
            )
            self.symbols = candidate_symbols
        else:
            raise ValueError("BROKER_MODE muss local oder alpaca sein.")

        if source == "alpaca" and settings.broker_mode != "alpaca":
            raise RuntimeError(
                "Der Alpaca-Datenfeed braucht BROKER_MODE=alpaca und Paper-Keys."
            )

        self.risk = RiskManager(
            settings.risk_per_trade,
            settings.max_position_fraction,
        )
        self.engine = StrategyEngine(
            settings.min_signal_confidence,
            settings.min_strategy_agreement,
        )
        self.candles = MinuteCandleStore(
            symbols=self.symbols,
            data_dir=settings.live_data_dir,
            max_bars=settings.live_max_bars,
            on_completed_bar=self.on_completed_bar,
        )

    def bootstrap(self) -> None:
        self.write_status(state="bootstrapping", force=True)
        self.candles.bootstrap(
            period=settings.live_history_period,
            interval=settings.live_history_interval,
        )

        for symbol in self.symbols:
            frame = self.candles.latest_frame(symbol)
            if not frame.empty:
                self.latest_prices[symbol] = float(frame.iloc[-1]["close"])
                self.last_processed_bar[symbol] = pd.Timestamp(frame.index[-1])
                self.last_bar[symbol] = str(frame.index[-1])

        self.write_status(state="waiting_for_live_data", force=True)

    def on_yahoo_message(self, message: dict) -> None:
        tick = self.candles.handle_message(message)
        if tick is None:
            return

        self.connected = True
        self.error = ""
        self.latest_prices[tick.symbol] = tick.price
        self.last_tick[tick.symbol] = str(tick.timestamp)

        closed = self.broker.update_positions({tick.symbol: tick.price})
        for symbol, pnl, reason in closed:
            logging.getLogger("live").info(
                "GESCHLOSSEN %s | PnL %.4f | %s",
                symbol,
                pnl,
                reason,
            )

        self.write_status(state="running")


    def on_alpaca_ready(self) -> None:
        self.connected = True
        self.error = ""
        logging.getLogger("live").info(
            "Alpaca-%s-WebSocket verbunden: %s",
            settings.alpaca_data_feed.upper(),
            ", ".join(self.symbols),
        )
        self.write_status(state="running", force=True)

    def on_alpaca_trade(self, tick: AlpacaTradeTick) -> None:
        if tick.symbol not in self.symbols:
            return
        self.connected = True
        self.error = ""
        self.latest_prices[tick.symbol] = tick.price
        self.last_tick[tick.symbol] = str(tick.timestamp)

        closed = self.broker.update_positions({tick.symbol: tick.price})
        for symbol, pnl, reason in closed:
            logging.getLogger("live").info(
                "GESCHLOSSEN %s | PnL %.4f | %s",
                symbol,
                pnl,
                reason,
            )
        self.write_status(state="running")

    def on_alpaca_bar(self, bar: AlpacaMinuteBar) -> None:
        if bar.symbol not in self.symbols:
            return
        self.connected = True
        self.error = ""
        self.latest_prices[bar.symbol] = bar.close
        self.last_tick[bar.symbol] = str(bar.timestamp)

        closed = self.broker.update_positions({bar.symbol: bar.close})
        for symbol, pnl, reason in closed:
            logging.getLogger("live").info(
                "GESCHLOSSEN %s | PnL %.4f | %s",
                symbol,
                pnl,
                reason,
            )

        self.candles.append_completed_bar(
            symbol=bar.symbol,
            timestamp=bar.timestamp,
            open_price=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        self.write_status(state="running", force=True)

    def on_completed_bar(self, symbol: str, frame: pd.DataFrame) -> None:
        if frame.empty:
            return

        bar_time = pd.Timestamp(frame.index[-1])
        previous = self.last_processed_bar.get(symbol)
        if previous is not None and bar_time <= previous:
            return

        self.last_processed_bar[symbol] = bar_time
        self.last_bar[symbol] = str(bar_time)
        self.latest_prices[symbol] = float(frame.iloc[-1]["close"])

        try:
            prepared = enrich(frame)
            if len(prepared) < 20:
                self.last_signal[symbol] = {
                    "direction": "hold",
                    "confidence": 0.0,
                    "reason": "Noch zu wenige fertige Minutenkerzen.",
                    "time": str(bar_time),
                }
                self.write_status(state="running", force=True)
                return

            signal_result = self.engine.analyze(symbol, prepared)
            self.last_signal[symbol] = {
                "direction": signal_result.direction.value,
                "confidence": signal_result.confidence,
                "strategy": signal_result.strategy,
                "reason": signal_result.reason,
                "time": str(bar_time),
            }

            logger = logging.getLogger("live")
            logger.info(
                "%s | %s | confidence %.2f | %s",
                symbol,
                signal_result.direction.value,
                signal_result.confidence,
                signal_result.strategy,
            )

            if signal_result.direction is Direction.HOLD:
                self.write_status(state="running", force=True)
                return

            if self.broker.has_position(symbol):
                self.write_status(state="running", force=True)
                return

            pending_count = len(getattr(self.broker, "pending_open", {}))
            if len(self.broker.positions) + pending_count >= settings.max_open_positions:
                logger.info("%s: maximale Zahl offener Positionen erreicht.", symbol)
                self.write_status(state="running", force=True)
                return

            if not getattr(self.broker, "order_execution_enabled", True):
                logger.info(
                    "%s: Alpaca-Paper ist verbunden, Orders sind aber noch gesperrt.",
                    symbol,
                )
                self.write_status(state="running", force=True)
                return

            equity = self.broker.equity(self.latest_prices)
            plan = self.risk.plan(
                signal_result,
                equity=equity,
                available_cash=self.broker.cash,
            )
            if plan is None:
                logger.info("%s: Risikoregel lehnt Signal ab.", symbol)
                self.write_status(state="running", force=True)
                return

            position = self.broker.open_position(
                signal_result,
                quantity=plan.quantity,
            )
            if position is not None:
                logger.info(
                    "ERÖFFNET %s %s | Wert %.2f | Risiko %.2f",
                    position.side,
                    symbol,
                    plan.notional,
                    plan.risk_amount,
                )

        except Exception as exc:
            self.error = f"{symbol}: Analysefehler: {exc}"
            logging.getLogger("live").exception(self.error)

        self.write_status(state="running", force=True)

    def run_alpaca_websocket(self) -> None:
        self.bootstrap()
        backoff = 3

        while self.running:
            stream: AlpacaStockStream | None = None
            try:
                self.connected = False
                self.write_status(state="connecting", force=True)
                stream = AlpacaStockStream(
                    api_key=settings.alpaca_api_key,
                    secret_key=settings.alpaca_secret_key,
                    symbols=self.symbols,
                    feed=settings.alpaca_data_feed,
                    on_trade=self.on_alpaca_trade,
                    on_bar=self.on_alpaca_bar,
                    on_ready=self.on_alpaca_ready,
                )
                self.active_stream = stream
                stream.run()
                if self.running:
                    raise ConnectionError(
                        stream.last_error or "Alpaca-WebSocket-Verbindung wurde beendet."
                    )

            except KeyboardInterrupt:
                self.running = False
            except Exception as exc:
                self.connected = False
                self.reconnects += 1
                self.error = str(exc)
                logging.getLogger("live").exception(
                    "Alpaca-Live-Verbindung abgebrochen. Neuer Versuch in %s Sekunden.",
                    backoff,
                )
                self.write_status(state="reconnecting", force=True)
                self._sleep_interruptibly(backoff)
                backoff = min(backoff * 2, settings.live_max_reconnect_seconds)
            finally:
                self.active_stream = None
                if stream is not None:
                    stream.close()

        self.shutdown()

    def run_yahoo_websocket(self) -> None:
        self.bootstrap()
        backoff = 3

        while self.running:
            websocket = None
            try:
                self.connected = False
                self.write_status(state="connecting", force=True)
                websocket = yf.WebSocket(verbose=False)
                websocket.subscribe(list(self.symbols))
                self.connected = True
                self.error = ""
                self.write_status(state="running", force=True)
                logging.getLogger("live").info(
                    "WebSocket verbunden: %s",
                    ", ".join(self.symbols),
                )
                websocket.listen(self.on_yahoo_message)

                if self.running:
                    raise ConnectionError("WebSocket-Verbindung wurde beendet.")

            except KeyboardInterrupt:
                self.running = False
            except Exception as exc:
                self.connected = False
                self.reconnects += 1
                self.error = f"WebSocket: {exc}"
                logging.getLogger("live").exception(
                    "Live-Verbindung abgebrochen. Neuer Versuch in %s Sekunden.",
                    backoff,
                )
                self.write_status(state="reconnecting", force=True)
                self._sleep_interruptibly(backoff)
                backoff = min(backoff * 2, settings.live_max_reconnect_seconds)
            finally:
                if websocket is not None:
                    try:
                        websocket.close()
                    except Exception:
                        pass

        self.shutdown()

    def run_polling(self) -> None:
        self.bootstrap()
        source = YahooMarketData()
        self.connected = True
        self.write_status(state="running_polling", force=True)

        while self.running:
            cycle_had_data = False

            for symbol in self.symbols:
                if not self.running:
                    break

                try:
                    frame = source.fetch(
                        symbol,
                        period=settings.live_history_period,
                        interval=settings.live_history_interval,
                    )
                    now_minute = pd.Timestamp.utcnow().tz_localize(None).floor("min")
                    completed = frame.loc[frame.index < now_minute].copy()
                    if completed.empty:
                        continue

                    cycle_had_data = True
                    latest_price = float(frame.iloc[-1]["close"])
                    latest_time = pd.Timestamp(frame.index[-1])
                    self.latest_prices[symbol] = latest_price
                    self.last_tick[symbol] = str(latest_time)
                    self.broker.update_positions({symbol: latest_price})

                    previous = self.last_processed_bar.get(symbol)
                    new_rows = (
                        completed
                        if previous is None
                        else completed.loc[completed.index > previous]
                    )

                    self.candles.frames[symbol] = frame.tail(
                        settings.live_max_bars
                    ).copy()
                    self.candles._save(symbol)

                    for bar_time in new_rows.index:
                        history = completed.loc[completed.index <= bar_time]
                        self.on_completed_bar(symbol, history)

                except Exception as exc:
                    self.error = f"{symbol}: Polling-Fehler: {exc}"
                    logging.getLogger("live").exception(self.error)

            self.connected = cycle_had_data
            self.write_status(
                state="running_polling" if cycle_had_data else "waiting_for_data",
                force=True,
            )
            self._sleep_interruptibly(settings.live_poll_seconds)

        self.shutdown()

    def write_status(
        self,
        *,
        state: str,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if (
            not force
            and now - self.last_status_write
            < settings.live_status_write_seconds
        ):
            return
        self.last_status_write = now

        positions = {
            symbol: asdict(position)
            for symbol, position in self.broker.positions.items()
        }
        equity = self.broker.equity(self.latest_prices)

        broker_summary = (
            self.broker.account_summary()
            if hasattr(self.broker, "account_summary")
            else {
                "broker": "local_simulation",
                "paper": True,
                "currency": "EUR",
                "orders_enabled": True,
            }
        )
        payload = {
            "state": state,
            "source": self.source,
            "broker": broker_summary,
            "connected": self.connected,
            "started_at": self.started_at,
            "updated_at": _utc_iso(),
            "last_tick": self.last_tick,
            "last_bar": self.last_bar,
            "prices": self.latest_prices,
            "signals": self.last_signal,
            "error": self.error,
            "reconnects": self.reconnects,
            "cash": self.broker.cash,
            "equity": equity,
            "realized_pnl": self.broker.realized_pnl,
            "positions": positions,
            "trade_history_count": len(self.broker.trade_history),
            "symbols": list(self.symbols),
        }
        _atomic_json_write(settings.live_status_path, payload)

    def shutdown(self) -> None:
        self.connected = False
        self.write_status(state="stopped", force=True)
        logging.getLogger("live").info("Live-Paper-Bot beendet.")

    def stop(self, *_: object) -> None:
        self.running = False
        if self.active_stream is not None:
            self.active_stream.close()

    def _sleep_interruptibly(self, seconds: int) -> None:
        end = time.monotonic() + seconds
        while self.running and time.monotonic() < end:
            time.sleep(min(0.5, max(end - time.monotonic(), 0)))


def configure_logging() -> None:
    settings.live_log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    file_handler = logging.FileHandler(
        settings.live_log_path,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp.replace(path)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reset_live_state() -> None:
    paths = [settings.live_status_path]
    if settings.broker_mode == "local":
        paths.append(settings.live_state_path)
    for path in paths:
        if path.exists():
            path.unlink()
    if settings.broker_mode == "alpaca":
        print(
            "Nur der lokale Status wurde gelöscht. Alpaca-Positionen und Orders "
            "werden absichtlich nicht zurückgesetzt."
        )
    else:
        print("Lokales Live-Paper-Konto und Status wurden zurückgesetzt.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live-Paper-Trading")
    parser.add_argument(
        "--source",
        choices=("alpaca", "yahoo", "websocket", "polling"),
        default="alpaca",
    )
    parser.add_argument("--reset", action="store_true")
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="Gespeicherte Scanner-Auswahl nutzen und keinen neuen Scan starten.",
    )
    return parser.parse_args()


def main() -> None:
    configure_logging()
    args = parse_args()

    if args.reset:
        reset_live_state()
        return

    if settings.scanner_enabled and not args.no_scan:
        print("Starte breiten Markt-Scanner. Das kann ein paar Minuten dauern...")
        try:
            summary = run_scan()
            print(
                f"Scanner fertig: {summary['analysed_count']} Märkte analysiert, "
                f"{summary['active_count']} live ausgewählt.\n"
            )
        except Exception as exc:
            logging.getLogger("live").exception(
                "Scanner fehlgeschlagen. Letzte Auswahl oder feste Symbole werden benutzt."
            )
            print(f"Scanner-Fehler: {exc}. Starte mit der letzten Auswahl.\n")

    source = args.source
    if source == "websocket":
        source = "alpaca" if settings.broker_mode == "alpaca" else "yahoo"

    runner = LivePaperRunner(source)
    signal.signal(signal.SIGINT, runner.stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, runner.stop)

    print("\nLIVE-PAPER-TRADING v0.9 CLOUD")
    print("Kein Echtgeld. Beenden mit Strg+C oder Fenster schließen.")
    print(f"Quelle: {runner.source}")
    print(f"Broker: {settings.broker_mode}")
    if settings.broker_mode == "alpaca":
        print(
            "Alpaca-Paper-Orders: "
            + ("AKTIV" if settings.alpaca_order_execution else "GESPERRT")
        )
    print(f"Symbole: {', '.join(runner.symbols)}\n")

    if source == "alpaca":
        runner.run_alpaca_websocket()
    elif source == "yahoo":
        runner.run_yahoo_websocket()
    else:
        runner.run_polling()


if __name__ == "__main__":
    main()
