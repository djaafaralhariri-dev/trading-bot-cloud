from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import html
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from app.alpaca_broker import AlpacaPaperBroker, AlpacaAPIError
from app.alpaca_history import AlpacaHistoricalBars, AlpacaMarketDataError
from app.config import BASE_DIR, settings
from app.indicators import enrich
from app.risk import RiskManager
from app.strategies import Direction, Signal, StrategyEngine


LOGGER = logging.getLogger("github_cloud")
SITE_DIR = BASE_DIR / "site"
RUNTIME_DIR = BASE_DIR / "runtime"
FALLBACK_SYMBOLS = (
    "SPY", "QQQ", "IWM", "XLF", "XLE", "XLK", "AAPL", "AMD", "F",
    "SOFI", "PLTR", "INTC", "BAC", "PFE", "NIO", "SNAP", "RIVN",
)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def load_cloud_symbols() -> tuple[str, ...]:
    path = BASE_DIR / "scanner_output" / "active_symbols.json"
    values: list[str] = []
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            values.extend(str(item).upper() for item in payload.get("symbols", []))
        except Exception:
            LOGGER.exception("Scanner-Auswahl konnte nicht gelesen werden.")
    if not values:
        values.extend(FALLBACK_SYMBOLS)
    return tuple(dict.fromkeys(item for item in values if item))


def build_broker() -> AlpacaPaperBroker:
    if settings.broker_mode != "alpaca":
        raise RuntimeError("BROKER_MODE muss für GitHub Cloud auf alpaca stehen.")
    if not settings.alpaca_paper:
        raise RuntimeError("ALPACA_PAPER muss true bleiben.")
    return AlpacaPaperBroker(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        state_path=RUNTIME_DIR / "alpaca_github_state.json",
        order_execution_enabled=settings.alpaca_order_execution,
        allow_shorts=False,
        max_order_notional=settings.alpaca_max_order_notional,
        refresh_seconds=2,
        require_fractionable=False,
    )


def make_client_order_id(symbol: str, timestamp: pd.Timestamp) -> str:
    raw = f"{symbol}|{pd.Timestamp(timestamp).isoformat()}|github-v1"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    safe_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum())[:8]
    return f"gh10-{safe_symbol}-{digest}"[:48]


def analyse_symbols(
    symbols: tuple[str, ...],
    histories: dict[str, pd.DataFrame],
) -> list[dict[str, Any]]:
    engine = StrategyEngine(
        settings.min_signal_confidence,
        settings.min_strategy_agreement,
    )
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        frame = histories.get(symbol)
        if frame is None or len(frame) < 60:
            rows.append(
                {
                    "symbol": symbol,
                    "direction": "-",
                    "confidence": 0.0,
                    "price": 0.0,
                    "bar_time": "-",
                    "strategy": "-",
                    "reason": "Zu wenige Alpaca-Minutenkerzen.",
                    "signal": None,
                }
            )
            continue
        try:
            prepared = enrich(frame)
            signal = engine.analyze(symbol, prepared)
            last_time = pd.Timestamp(prepared.index[-1])
            rows.append(
                {
                    "symbol": symbol,
                    "direction": signal.direction.value,
                    "confidence": float(signal.confidence),
                    "price": float(signal.entry_price),
                    "bar_time": last_time.isoformat(),
                    "strategy": signal.strategy,
                    "reason": signal.reason,
                    "signal": signal,
                    "timestamp": last_time,
                }
            )
        except Exception as exc:
            LOGGER.exception("Analyse fehlgeschlagen: %s", symbol)
            rows.append(
                {
                    "symbol": symbol,
                    "direction": "error",
                    "confidence": 0.0,
                    "price": 0.0,
                    "bar_time": "-",
                    "strategy": "-",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "signal": None,
                }
            )
    rows.sort(key=lambda item: float(item.get("confidence", 0.0)), reverse=True)
    return rows


def run_once(*, dashboard_only: bool, ignore_market_clock: bool) -> dict[str, Any]:
    configure_logging()
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    broker = build_broker()
    account = broker.account_summary()
    clock = broker.market_clock()
    is_open = bool(clock.get("is_open", False))
    candidate_symbols = load_cloud_symbols()
    tradable_symbols = broker.filter_tradable_symbols(candidate_symbols)

    histories: dict[str, pd.DataFrame] = {}
    signal_rows: list[dict[str, Any]] = []
    submitted: list[dict[str, Any]] = []
    errors: list[str] = []

    should_fetch = is_open or ignore_market_clock or dashboard_only
    if should_fetch:
        try:
            history_client = AlpacaHistoricalBars(
                api_key=settings.alpaca_api_key,
                secret_key=settings.alpaca_secret_key,
                feed=settings.alpaca_data_feed,
            )
            histories = history_client.fetch_recent(
                tradable_symbols,
                lookback_days=4,
                timeframe="1Min",
                max_bars_per_symbol=600,
            )
            signal_rows = analyse_symbols(tradable_symbols, histories)
        except (AlpacaMarketDataError, AlpacaAPIError, RuntimeError) as exc:
            errors.append(str(exc))
            LOGGER.exception("Marktdaten/Analyse fehlgeschlagen.")

    if (
        not dashboard_only
        and (is_open or ignore_market_clock)
        and signal_rows
    ):
        risk = RiskManager(settings.risk_per_trade, settings.max_position_fraction)
        broker.refresh(force=True)
        occupied = set(broker.positions)
        occupied.update(broker.open_order_symbols())
        available_slots = max(0, settings.max_open_positions - len(occupied))

        actionable = [
            row for row in signal_rows
            if isinstance(row.get("signal"), Signal)
            and row["signal"].direction is Direction.LONG
        ]
        for row in actionable:
            if available_slots <= 0:
                break
            symbol = str(row["symbol"])
            signal: Signal = row["signal"]
            if symbol in occupied:
                continue
            if signal.entry_price > settings.alpaca_max_order_notional:
                row["cloud_note"] = "Eine ganze Aktie ist teurer als das Orderlimit."
                continue

            plan = risk.plan(
                signal,
                equity=broker.equity(),
                available_cash=broker.cash,
            )
            if plan is None:
                row["cloud_note"] = "Risikoregel lehnt den Trade ab."
                continue
            whole_qty = math.floor(plan.quantity)
            if whole_qty < 1:
                row["cloud_note"] = "Für einen serverseitig geschützten Bracket-Trade ist die Stückzahl unter 1."
                continue

            client_order_id = make_client_order_id(symbol, row["timestamp"])
            try:
                position = broker.open_bracket_position(
                    signal,
                    quantity=float(whole_qty),
                    client_order_id=client_order_id,
                )
            except AlpacaAPIError as exc:
                errors.append(f"{symbol}: {exc}")
                row["cloud_note"] = f"Orderfehler: {exc}"
                continue
            if position is None:
                row["cloud_note"] = "Keine Order gesendet."
                continue

            submitted.append(
                {
                    "symbol": symbol,
                    "quantity": position.quantity,
                    "entry": position.entry_price,
                    "stop": position.stop_loss,
                    "target": position.take_profit,
                    "strategy": position.strategy,
                    "client_order_id": client_order_id,
                }
            )
            occupied.add(symbol)
            available_slots -= 1

    try:
        raw_positions = broker.raw_positions()
        raw_orders = broker.raw_orders(status="all", limit=25, nested=True)
        account = broker.account_summary()
    except AlpacaAPIError as exc:
        errors.append(str(exc))
        raw_positions = []
        raw_orders = []

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paper": True,
        "source": f"Alpaca {settings.alpaca_data_feed.upper()} REST",
        "account": account,
        "clock": clock,
        "candidate_count": len(candidate_symbols),
        "tradable_count": len(tradable_symbols),
        "analysed_count": len(histories),
        "orders_enabled": settings.alpaca_order_execution,
        "submitted": submitted,
        "positions": raw_positions,
        "orders": raw_orders,
        "signals": [_serialise_signal_row(row) for row in signal_rows],
        "errors": errors,
    }
    (RUNTIME_DIR / "latest_run.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (SITE_DIR / "index.html").write_text(render_dashboard(payload), encoding="utf-8")
    return payload


def _serialise_signal_row(row: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in row.items() if key not in {"signal", "timestamp"}}
    signal = row.get("signal")
    if isinstance(signal, Signal):
        result["stop_loss"] = signal.stop_loss
        result["take_profit"] = signal.take_profit
    return result


def render_dashboard(payload: dict[str, Any]) -> str:
    account = payload.get("account", {}) or {}
    clock = payload.get("clock", {}) or {}
    positions = payload.get("positions", []) or []
    orders = payload.get("orders", []) or []
    signals = payload.get("signals", []) or []
    errors = payload.get("errors", []) or []
    submitted = payload.get("submitted", []) or []

    def money(value: Any) -> str:
        try:
            return f"{float(value):,.2f} USD"
        except (TypeError, ValueError):
            return "-"

    def num(value: Any, digits: int = 2) -> str:
        try:
            return f"{float(value):,.{digits}f}"
        except (TypeError, ValueError):
            return "-"

    position_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('symbol', '-')))}</td>"
        f"<td>{html.escape(str(item.get('side', '-')))}</td>"
        f"<td>{num(item.get('qty'), 4)}</td>"
        f"<td>{money(item.get('market_value'))}</td>"
        f"<td>{money(item.get('unrealized_pl'))}</td>"
        "</tr>"
        for item in positions
    ) or '<tr><td colspan="5" class="muted">Keine offenen Paper-Positionen.</td></tr>'

    order_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('symbol', '-')))}</td>"
        f"<td>{html.escape(str(item.get('side', '-')))}</td>"
        f"<td>{html.escape(str(item.get('order_class') or 'simple'))}</td>"
        f"<td>{html.escape(str(item.get('status', '-')))}</td>"
        f"<td>{html.escape(str(item.get('submitted_at', '-')))}</td>"
        "</tr>"
        for item in orders[:15]
    ) or '<tr><td colspan="5" class="muted">Noch keine Paper-Orders.</td></tr>'

    signal_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('symbol', '-')))}</td>"
        f"<td>{num(item.get('price'), 4)}</td>"
        f"<td>{html.escape(str(item.get('direction', '-')))}</td>"
        f"<td>{num(float(item.get('confidence', 0)) * 100, 1)}%</td>"
        f"<td>{html.escape(str(item.get('strategy', '-')))}</td>"
        f"<td>{html.escape(str(item.get('cloud_note') or item.get('reason', '-')))}</td>"
        "</tr>"
        for item in signals[:25]
    ) or '<tr><td colspan="6" class="muted">Noch keine Analyse vorhanden.</td></tr>'

    error_block = ""
    if errors:
        error_block = '<section class="alert error"><strong>Fehler</strong><br>' + "<br>".join(
            html.escape(str(item)) for item in errors
        ) + "</section>"
    submitted_block = ""
    if submitted:
        submitted_block = '<section class="alert success"><strong>Neue Paper-Bracket-Orders:</strong> ' + ", ".join(
            f"{html.escape(str(item.get('symbol')))} ({num(item.get('quantity'), 0)} Stück)"
            for item in submitted
        ) + "</section>"

    market_text = "Offen" if bool(clock.get("is_open")) else "Geschlossen"
    orders_text = "AKTIV" if bool(payload.get("orders_enabled")) else "GESPERRT"
    updated = html.escape(str(payload.get("generated_at", "-")))

    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Trading-Bot Paper Dashboard</title>
<style>
:root {{ color-scheme: dark; --bg:#0c0f14; --panel:#151922; --line:#2a3040; --text:#f1f4f8; --muted:#9aa4b2; --accent:#f6c945; --good:#63d69d; --bad:#ff7b7b; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif; }}
main {{ max-width:1180px; margin:auto; padding:24px; }} h1 {{ font-size:34px; margin:0 0 4px; }} h2 {{ margin-top:28px; }} .muted {{ color:var(--muted); }}
.banner {{ border:1px solid #695d1c; background:#2d2910; padding:12px 14px; border-radius:10px; margin:18px 0; }}
.grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }} .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }}
.label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }} .value {{ font-size:25px; margin-top:4px; overflow-wrap:anywhere; }}
.table-wrap {{ overflow:auto; border:1px solid var(--line); border-radius:12px; }} table {{ width:100%; border-collapse:collapse; min-width:760px; }} th,td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }} th {{ background:#1a1f2a; position:sticky; top:0; }}
.alert {{ padding:12px 14px; border-radius:10px; margin:12px 0; }} .error {{ background:#351719; border:1px solid #7b3036; }} .success {{ background:#123024; border:1px solid #296b4b; }}
footer {{ color:var(--muted); margin:28px 0 10px; font-size:13px; }}
@media (max-width:800px) {{ main {{ padding:14px; }} .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} h1 {{ font-size:28px; }} .value {{ font-size:20px; }} }}
</style>
</head>
<body><main>
<h1>Trading-Bot</h1>
<div class="muted">GitHub Cloud · Alpaca Paper · kein Echtgeld</div>
<div class="banner">Der Bot läuft nicht dauerhaft als WebSocket. GitHub startet ihn planmäßig, analysiert Alpaca-Minutenkurse und legt Stop-Loss sowie Take-Profit als Bracket-Order direkt bei Alpaca ab.</div>
{error_block}{submitted_block}
<div class="grid">
<div class="card"><div class="label">Paper-Equity</div><div class="value">{money(account.get('equity'))}</div></div>
<div class="card"><div class="label">Freies Cash</div><div class="value">{money(account.get('cash'))}</div></div>
<div class="card"><div class="label">US-Markt</div><div class="value">{market_text}</div></div>
<div class="card"><div class="label">Paper-Orders</div><div class="value">{orders_text}</div></div>
<div class="card"><div class="label">Scanner-Kandidaten</div><div class="value">{int(payload.get('candidate_count', 0))}</div></div>
<div class="card"><div class="label">Bei Alpaca handelbar</div><div class="value">{int(payload.get('tradable_count', 0))}</div></div>
<div class="card"><div class="label">Analysiert</div><div class="value">{int(payload.get('analysed_count', 0))}</div></div>
<div class="card"><div class="label">Kursquelle</div><div class="value">{html.escape(str(payload.get('source', '-')))}</div></div>
</div>
<h2>Offene Paper-Positionen</h2><div class="table-wrap"><table><thead><tr><th>Markt</th><th>Richtung</th><th>Menge</th><th>Marktwert</th><th>Unrealisiert</th></tr></thead><tbody>{position_rows}</tbody></table></div>
<h2>Letzte Paper-Orders</h2><div class="table-wrap"><table><thead><tr><th>Markt</th><th>Seite</th><th>Klasse</th><th>Status</th><th>Zeit</th></tr></thead><tbody>{order_rows}</tbody></table></div>
<h2>Letzte Analyse</h2><div class="table-wrap"><table><thead><tr><th>Markt</th><th>Preis</th><th>Signal</th><th>Vertrauen</th><th>Strategie</th><th>Begründung</th></tr></thead><tbody>{signal_rows}</tbody></table></div>
<footer>Letzte Aktualisierung: {updated}. Die GitHub-Zeitplanung kann sich verspäten. Der Alpaca-Paper-Endpunkt ist im Code fest verdrahtet.</footer>
</main></body></html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Einmaliger GitHub-Cloud-Paperlauf")
    parser.add_argument("--dashboard-only", action="store_true")
    parser.add_argument("--ignore-market-clock", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = run_once(
        dashboard_only=args.dashboard_only,
        ignore_market_clock=args.ignore_market_clock,
    )
    print(
        "GitHub-Cloudlauf fertig | "
        f"Equity {payload.get('account', {}).get('equity', '-')} | "
        f"analysiert {payload.get('analysed_count', 0)} | "
        f"Orders {len(payload.get('submitted', []))} | "
        f"Ausführung {'AN' if payload.get('orders_enabled') else 'AUS'}"
    )


if __name__ == "__main__":
    main()
