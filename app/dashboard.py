from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from app.config import BASE_DIR, settings


st.set_page_config(
    page_title="Trading-Bot",
    page_icon="📈",
    layout="wide",
)

BACKTEST_DIR = BASE_DIR / "backtest_output"
PORTFOLIO_DIR = BASE_DIR / "portfolio_backtest_output"


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception as exc:
        st.error(f"{path.name} konnte nicht gelesen werden: {exc}")
        return pd.DataFrame()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def money(value: float, currency: str = "EUR") -> str:
    symbol = "$" if currency.upper() == "USD" else "€"
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{symbol}{formatted}" if symbol == "$" else f"{formatted} {symbol}"


def pct(value: float) -> str:
    return f"{value:.2f} %".replace(".", ",")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def status_label(state: str, connected: bool) -> tuple[str, str]:
    if connected and state.startswith("running"):
        return "🟢 Läuft", "Der Bot empfängt Daten und überwacht das Paper-Konto."
    if state in {"connecting", "reconnecting", "bootstrapping"}:
        return "🟡 Verbindet", "Der Bot baut die Datenverbindung auf."
    if state in {"waiting_for_live_data", "waiting_for_data"}:
        return "🟡 Wartet", "Gerade kommen keine neuen Kurse, etwa weil ein Markt geschlossen ist."
    if state == "stopped":
        return "🔴 Gestoppt", "Das Live-Paper-Fenster ist geschlossen."
    return "⚪ Noch nicht gestartet", "Starte zuerst run_live_paper.bat."


def live_page_content() -> None:
    status = load_json(settings.live_status_path)
    broker_info = status.get("broker", {}) if status else {}
    broker_mode = str(broker_info.get("broker", "local_simulation"))
    state_path = (
        settings.alpaca_state_path
        if broker_mode == "alpaca_paper"
        else settings.live_state_path
    )
    state_file = load_json(state_path)

    st.header("Live-Paper-Trading")
    st.caption(
        "Paper-Trading mit echten Marktdaten. Es wird kein Echtgeld verwendet."
    )

    if not status:
        st.warning(
            "Der Live-Bot wurde noch nicht gestartet. "
            "Doppelklicke im Projektordner auf run_live_paper.bat."
        )
        return

    state = str(status.get("state", ""))
    connected = bool(status.get("connected", False))
    label, explanation = status_label(state, connected)

    col_status, col_source, col_broker, col_update = st.columns(4)
    col_status.metric("Status", label)
    source = str(status.get("source", "-"))
    source_labels = {
        "alpaca_iex": "Alpaca IEX WebSocket",
        "alpaca_sip": "Alpaca SIP WebSocket",
        "alpaca_delayed_sip": "Alpaca Delayed SIP",
        "yahoo": "Yahoo WebSocket",
        "websocket": "Yahoo WebSocket",
        "polling": "Yahoo 1-Minuten-Abruf",
    }
    col_source.metric("Kursquelle", source_labels.get(source, source))
    col_broker.metric(
        "Paper-Broker",
        "Alpaca Paper" if broker_mode == "alpaca_paper" else "Lokale Simulation",
    )
    col_update.metric("Letzte Aktualisierung", str(status.get("updated_at", "-"))[:19])
    st.caption(explanation)

    currency = str(broker_info.get("currency", "EUR"))
    starting = settings.starting_cash
    equity = safe_float(status.get("equity"), starting)
    cash = safe_float(status.get("cash"), starting)
    realized = safe_float(status.get("realized_pnl"), 0)
    positions = status.get("positions", {}) or {}

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Virtuelles Kapital", money(equity, currency), money(equity - starting, currency))
    col2.metric("Freies Cash", money(cash, currency))
    pnl_label = "Heutiger P/L" if broker_mode == "alpaca_paper" else "Realisierter Gewinn/Verlust"
    col3.metric(pnl_label, money(realized, currency))
    col4.metric("Offene Positionen", len(positions))

    if broker_mode == "alpaca_paper":
        account = str(broker_info.get("account", "-"))
        if bool(broker_info.get("orders_enabled", False)):
            st.success(
                f"Mit Alpaca PAPER verbunden ({account}). Paper-Orders sind aktiviert."
            )
        else:
            st.warning(
                f"Mit Alpaca PAPER verbunden ({account}), aber Orders sind noch gesperrt. "
                "Starte enable_alpaca_orders.bat erst nach dem Verbindungstest."
            )

    error = str(status.get("error", "")).strip()
    if error:
        st.error(error)

    scanner_summary = load_json(settings.scanner_summary_path)
    if scanner_summary:
        with st.expander("Markt-Scanner", expanded=False):
            col_scan1, col_scan2, col_scan3 = st.columns(3)
            col_scan1.metric("Kandidaten", int(scanner_summary.get("candidate_count", 0)))
            col_scan2.metric("Analysiert", int(scanner_summary.get("analysed_count", 0)))
            col_scan3.metric("Live ausgewählt", int(scanner_summary.get("active_count", 0)))
            st.caption(
                "Der Scanner prüft breit und übergibt nur die bestbewerteten Märkte "
                "an den Live-Bot. Er scannt nicht jede Börse der Erde gleichzeitig."
            )

    prices = status.get("prices", {}) or {}
    last_ticks = status.get("last_tick", {}) or {}
    signals = status.get("signals", {}) or {}

    rows = []
    for symbol in status.get("symbols", settings.symbols):
        signal_info = signals.get(symbol, {})
        rows.append(
            {
                "Markt": symbol,
                "Letzter Preis": prices.get(symbol),
                "Letzter Tick": last_ticks.get(symbol, "-"),
                "Signal": signal_info.get("direction", "-"),
                "Vertrauen": round(safe_float(signal_info.get("confidence")) * 100, 1),
                "Begründung": signal_info.get("reason", "Noch keine fertige Live-Minute analysiert."),
            }
        )

    st.subheader("Was sieht der Bot gerade?")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    symbol = st.selectbox("Live-Chart auswählen", list(status.get("symbols", settings.symbols)))
    safe_symbol = symbol.replace("=", "_").replace("^", "_").replace("/", "_")
    live_frame = load_csv(settings.live_data_dir / f"{safe_symbol}_1m.csv")

    if not live_frame.empty and {"time", "close"}.issubset(live_frame.columns):
        live_frame["time"] = pd.to_datetime(live_frame["time"], errors="coerce")
        live_frame = live_frame.dropna(subset=["time"]).tail(500)
        fig = px.line(
            live_frame,
            x="time",
            y="close",
            title=f"{symbol}: letzte 1-Minuten-Kerzen",
            labels={"time": "Zeit", "close": "Preis"},
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Für diesen Markt sind noch keine Minutenkerzen gespeichert.")

    st.subheader(
        "Offene Alpaca-Paper-Positionen"
        if broker_mode == "alpaca_paper"
        else "Offene virtuelle Positionen"
    )
    if positions:
        position_rows = []
        for symbol_name, position in positions.items():
            position_rows.append(
                {
                    "Markt": symbol_name,
                    "Richtung": position.get("side"),
                    "Menge": position.get("quantity"),
                    "Einstieg": position.get("entry_price"),
                    "Stop": position.get("stop_loss"),
                    "Ziel": position.get("take_profit"),
                    "Strategie": position.get("strategy"),
                }
            )
        st.dataframe(pd.DataFrame(position_rows), use_container_width=True, hide_index=True)
    else:
        st.info("Aktuell ist keine virtuelle Position offen. Das ist kein Fehler.")

    trade_history = state_file.get("trade_history", []) if state_file else []
    st.subheader("Letzte Live-Paper-Aktionen")
    if trade_history:
        st.dataframe(
            pd.DataFrame(trade_history).tail(30).iloc[::-1],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Noch keine Live-Paper-Trades ausgeführt.")


def show_live_page() -> None:
    if hasattr(st, "fragment"):
        fragment = st.fragment(run_every="5s")(live_page_content)
        fragment()
    else:
        if st.button("Aktualisieren"):
            st.rerun()
        live_page_content()


def simple_backtest_page() -> None:
    st.header("Was hat der bisherige Test ergeben?")
    summary = load_csv(PORTFOLIO_DIR / "summary.csv")
    trades = load_csv(PORTFOLIO_DIR / "trades.csv")
    side = load_csv(BACKTEST_DIR / "breakdown_by_side.csv")

    if summary.empty:
        st.warning("Noch kein Portfolio-Backtest vorhanden.")
        return

    row = summary.iloc[0]
    start = safe_float(row.get("starting_cash"), 250)
    end = safe_float(row.get("final_equity"), start)
    total_return = safe_float(row.get("total_return_pct"))
    drawdown = safe_float(row.get("max_drawdown_pct"))
    fees = safe_float(row.get("total_fees"))

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Start", money(start))
    col2.metric("Ende nach 5 Jahren", money(end), money(end - start))
    col3.metric("Gesamtrendite", pct(total_return))
    col4.metric("Größter Zwischenverlust", pct(drawdown))

    st.subheader("Klare Bewertung")
    if total_return > 0:
        st.success(
            f"Der historische Test war profitabel: {money(end - start)} Gewinn. "
            "Für fünf Jahre ist die Rendite aber noch zu niedrig für Echtgeld."
        )
    else:
        st.error("Der historische Test verlor Geld. Kein Echtgeld.")

    if drawdown > 10:
        st.warning(
            f"Das Konto fiel zwischenzeitlich um {pct(drawdown)}. "
            "Das Risiko ist im Verhältnis zur Rendite zu hoch."
        )

    st.write(
        f"Die simulierten Gebühren betrugen **{money(fees)}**. "
        "Viele kleine Trades fressen dadurch einen großen Teil des Vorteils."
    )

    if not trades.empty and {"symbol", "pnl"}.issubset(trades.columns):
        trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce").fillna(0)
        markets = (
            trades.groupby("symbol", as_index=False)
            .agg(Gewinn=("pnl", "sum"), Trades=("symbol", "size"))
            .sort_values("Gewinn", ascending=False)
        )
        best = markets.iloc[0]
        worst = markets.iloc[-1]
        col_good, col_bad = st.columns(2)
        col_good.success(
            f"Stärkster Markt: {best['symbol']} mit {money(float(best['Gewinn']))}"
        )
        col_bad.error(
            f"Schwächster Markt: {worst['symbol']} mit {money(float(worst['Gewinn']))}"
        )

        fig = px.bar(
            markets,
            x="symbol",
            y="Gewinn",
            title="Gewinn und Verlust je Markt",
            labels={"symbol": "Markt"},
        )
        st.plotly_chart(fig, use_container_width=True)

    if not side.empty and {"side", "net_pnl"}.issubset(side.columns):
        long_row = side.loc[side["side"] == "long"]
        short_row = side.loc[side["side"] == "short"]
        if not long_row.empty and not short_row.empty:
            long_pnl = safe_float(long_row.iloc[0]["net_pnl"])
            short_pnl = safe_float(short_row.iloc[0]["net_pnl"])
            st.write(
                f"**Long-Trades:** {money(long_pnl)}  \n"
                f"**Short-Trades:** {money(short_pnl)}"
            )
            if short_pnl < 0:
                st.warning("Die aktuelle Short-Logik verliert Geld und muss überarbeitet werden.")


def scanner_page() -> None:
    st.header("Markt-Scanner")
    st.caption(
        "Breiter Vorauswahl-Scan für Aktien, ETFs, Krypto und Forex. "
        "Danach beobachtet der Live-Bot nur die besten Kandidaten."
    )

    summary = load_json(settings.scanner_summary_path)
    ranked = load_csv(settings.scanner_ranked_path)
    selected = load_csv(settings.scanner_selected_path)

    if not summary:
        st.warning(
            "Noch kein Scan vorhanden. Starte run_market_scanner.bat oder den Live-Bot."
        )
        return

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Kandidaten", int(summary.get("candidate_count", 0)))
    col2.metric("Kursdaten erhalten", int(summary.get("downloaded_count", 0)))
    col3.metric("Analysiert", int(summary.get("analysed_count", 0)))
    col4.metric("Live-Auswahl", int(summary.get("active_count", 0)))

    st.write(f"Letzter Scan: **{str(summary.get('finished_at', '-'))[:19]} UTC**")
    st.info(
        "Das ist ein technischer Markt-Scanner, kein Internet- oder News-Scanner. "
        "Er bewertet Kursdaten, Liquidität, Trend, Momentum, Volatilität und Signale."
    )

    if not selected.empty:
        columns = [
            "rank", "symbol", "asset_label", "score", "signal", "confidence",
            "price", "return_5_pct", "return_20_pct", "atr_pct", "rsi_14",
        ]
        shown = selected[[column for column in columns if column in selected.columns]].copy()
        shown = shown.rename(
            columns={
                "rank": "Rang",
                "symbol": "Markt",
                "asset_label": "Anlageklasse",
                "score": "Score",
                "signal": "Signal",
                "confidence": "Vertrauen",
                "price": "Preis",
                "return_5_pct": "5 Tage %",
                "return_20_pct": "20 Tage %",
                "atr_pct": "ATR %",
                "rsi_14": "RSI",
            }
        )
        if "Vertrauen" in shown.columns:
            shown["Vertrauen"] = pd.to_numeric(shown["Vertrauen"], errors="coerce") * 100
        st.subheader("Aktive Auswahl")
        st.dataframe(shown, use_container_width=True, hide_index=True)
    else:
        st.warning("Der letzte Scan konnte keine brauchbaren Märkte auswählen.")

    if not ranked.empty:
        st.subheader("Komplette Rangliste")
        st.dataframe(ranked, use_container_width=True, hide_index=True)

    errors = summary.get("errors", []) or []
    if errors:
        with st.expander(f"Übersprungene Datenquellen/Kandidaten ({len(errors)})"):
            for item in errors[:50]:
                st.code(str(item))


def details_page() -> None:
    st.header("Backtest-Details")
    tabs = st.tabs(["Märkte", "Strategien", "Walk-Forward", "Portfolio-Trades"])

    with tabs[0]:
        data = load_csv(BACKTEST_DIR / "summary.csv")
        st.dataframe(data, use_container_width=True, hide_index=True)

    with tabs[1]:
        strategies = load_csv(BACKTEST_DIR / "breakdown_by_strategy.csv")
        sides = load_csv(BACKTEST_DIR / "breakdown_by_side.csv")
        st.subheader("Strategien")
        st.dataframe(strategies, use_container_width=True, hide_index=True)
        st.subheader("Long gegen Short")
        st.dataframe(sides, use_container_width=True, hide_index=True)

    with tabs[2]:
        walk = load_csv(BACKTEST_DIR / "walk_forward.csv")
        st.caption(
            "Jeder Abschnitt wird getrennt betrachtet. So erkennen wir, "
            "ob der Bot nur in einem glücklichen Zeitraum funktioniert."
        )
        st.dataframe(walk, use_container_width=True, hide_index=True)

    with tabs[3]:
        trades = load_csv(PORTFOLIO_DIR / "trades.csv")
        st.dataframe(trades, use_container_width=True, hide_index=True)


def main() -> None:
    st.title("Trading-Bot")
    st.caption("Backtesting und Live-Paper-Trading. Kein Echtgeld.")

    page = st.sidebar.radio(
        "Bereich",
        [
            "Live-Paper",
            "Markt-Scanner",
            "Einfach erklärt",
            "Backtest-Details",
        ],
    )

    if page == "Live-Paper":
        show_live_page()
    elif page == "Markt-Scanner":
        scanner_page()
    elif page == "Einfach erklärt":
        simple_backtest_page()
    else:
        details_page()


if __name__ == "__main__":
    main()
