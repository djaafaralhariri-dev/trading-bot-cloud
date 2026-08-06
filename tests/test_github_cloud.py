from __future__ import annotations

import pandas as pd

from app.github_cloud import make_client_order_id, render_dashboard


def test_client_order_id_is_deterministic() -> None:
    timestamp = pd.Timestamp("2026-08-06T13:30:00")
    first = make_client_order_id("AAPL", timestamp)
    second = make_client_order_id("AAPL", timestamp)
    assert first == second
    assert first.startswith("gh10-AAPL-")
    assert len(first) <= 48


def test_dashboard_masks_to_payload_and_says_paper() -> None:
    page = render_dashboard(
        {
            "generated_at": "2026-08-06T13:30:00+00:00",
            "source": "Alpaca IEX REST",
            "orders_enabled": False,
            "candidate_count": 10,
            "tradable_count": 8,
            "analysed_count": 8,
            "account": {"cash": 250, "equity": 250},
            "clock": {"is_open": True},
            "positions": [],
            "orders": [],
            "signals": [],
            "errors": [],
            "submitted": [],
        }
    )
    assert "kein Echtgeld" in page
    assert "250.00 USD" in page
    assert "GESPERRT" in page
