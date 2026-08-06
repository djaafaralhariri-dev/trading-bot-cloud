from __future__ import annotations

from app.alpaca_broker import AlpacaPaperBroker
from app.config import settings


def main() -> None:
    broker = AlpacaPaperBroker(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        state_path=settings.alpaca_state_path,
        order_execution_enabled=False,
        allow_shorts=False,
        max_order_notional=settings.alpaca_max_order_notional,
        refresh_seconds=settings.alpaca_refresh_seconds,
        require_fractionable=settings.alpaca_require_fractionable,
    )
    summary = broker.account_summary()
    print("\nVERBINDUNG ERFOLGREICH")
    print("Umgebung: Alpaca PAPER (fest verdrahtet)")
    print(f"Konto: {summary['account']}")
    print(f"Status: {summary['status']}")
    print(f"Währung: {summary['currency']}")
    print(f"Cash: {summary['cash']:.2f}")
    print(f"Equity: {summary['equity']:.2f}")
    print("Orders bleiben bis enable_alpaca_orders.bat gesperrt.")


if __name__ == "__main__":
    main()
