from __future__ import annotations

import argparse
import logging
import sys
import time

from app.config import settings
from app.indicators import enrich
from app.market_data import YahooMarketData
from app.paper_broker import PaperBroker
from app.risk import RiskManager
from app.strategies import Direction, StrategyEngine


def configure_logging() -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.FileHandler(settings.log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def run_cycle(
    data_source: YahooMarketData,
    engine: StrategyEngine,
    risk: RiskManager,
    broker: PaperBroker,
) -> None:
    logger = logging.getLogger("cycle")
    logger.info("Scanne: %s", ", ".join(settings.symbols))

    frames = data_source.fetch_many(
        settings.symbols,
        period=settings.data_period,
        interval=settings.data_interval,
    )
    if not frames:
        logger.error("Keine Marktdaten verfügbar.")
        return

    prepared_data = {}
    prices: dict[str, float] = {}

    for symbol, frame in frames.items():
        try:
            prepared = enrich(frame)
            if prepared.empty:
                logger.warning("%s: keine verwertbaren Indikatordaten.", symbol)
                continue
            prepared_data[symbol] = prepared
            prices[symbol] = float(prepared.iloc[-1]["close"])
        except Exception:
            logger.exception("%s: Indikatorberechnung fehlgeschlagen.", symbol)

    for symbol, pnl, reason in broker.update_positions(prices):
        logger.info(
            "%s geschlossen | PnL %.2f | %s",
            symbol,
            pnl,
            reason,
        )

    equity = broker.equity(prices)

    for symbol, prepared in prepared_data.items():
        if broker.has_position(symbol):
            continue
        if len(broker.positions) >= settings.max_open_positions:
            break

        signal = engine.analyze(symbol, prepared)
        logger.info(
            "%s | %s | confidence %.2f | %s",
            symbol,
            signal.direction.value,
            signal.confidence,
            signal.strategy,
        )

        if signal.direction is Direction.HOLD:
            continue

        plan = risk.plan(
            signal,
            equity=equity,
            available_cash=broker.cash,
        )
        if plan is None:
            logger.info("%s: Risikoregel lehnt Trade ab.", symbol)
            continue

        position = broker.open_position(signal, plan.quantity)
        if position is not None:
            logger.info(
                "%s eröffnet | Wert %.2f | Risiko %.2f",
                symbol,
                plan.notional,
                plan.risk_amount,
            )
            equity = broker.equity(prices)

    print_summary(broker, prices)


def print_summary(
    broker: PaperBroker,
    prices: dict[str, float],
) -> None:
    equity = broker.equity(prices)
    print("\n" + "=" * 64)
    print("PAPER-TRADING-ZUSAMMENFASSUNG")
    print(f"Cash:              {broker.cash:10.2f}")
    print(f"Equity:            {equity:10.2f}")
    print(f"Realisierter PnL:  {broker.realized_pnl:10.2f}")
    print(f"Offene Positionen: {len(broker.positions):10d}")

    for position in broker.positions.values():
        current = prices.get(position.symbol, position.entry_price)
        pnl = position.unrealized_pnl(current)
        print(
            f"  {position.symbol:12} {position.side:5} "
            f"Entry {position.entry_price:10.4f} "
            f"Jetzt {current:10.4f} "
            f"PnL {pnl:8.2f}"
        )
    print("=" * 64 + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Trading Bot v0.1")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    parser.add_argument("--reset", action="store_true")
    return parser.parse_args()


def main() -> None:
    configure_logging()
    args = parse_args()

    data_source = YahooMarketData()
    engine = StrategyEngine(
        settings.min_signal_confidence,
        settings.min_strategy_agreement,
    )
    risk = RiskManager(
        settings.risk_per_trade,
        settings.max_position_fraction,
    )
    broker = PaperBroker(
        settings.starting_cash,
        settings.fee_rate,
        settings.slippage_rate,
        settings.state_path,
    )

    if args.reset:
        broker.reset()
        print("Paper-Konto wurde zurückgesetzt.")
        return

    if args.loop:
        while True:
            try:
                run_cycle(data_source, engine, risk, broker)
            except KeyboardInterrupt:
                print("\nBot wurde beendet.")
                return
            except Exception:
                logging.getLogger("main").exception(
                    "Unerwarteter Fehler im Hauptzyklus."
                )
            time.sleep(settings.loop_seconds)
    else:
        run_cycle(data_source, engine, risk, broker)


if __name__ == "__main__":
    main()
