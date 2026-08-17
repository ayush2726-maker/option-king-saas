import sqlite3
from datetime import datetime, timedelta, timezone

from bot import trade_live_routes as routes


def _open_trade_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            symbol TEXT,
            side TEXT,
            entry_price REAL,
            qty INTEGER,
            pnl REAL,
            status TEXT,
            reason TEXT,
            sl_price REAL,
            target_price REAL,
            last_ltp REAL,
            capital_slot INTEGER,
            trading_mode TEXT,
            created_at TEXT,
            quote_updated_at TEXT,
            quote_source TEXT
        )
        """
    )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn.executemany(
        """
        INSERT INTO paper_trades (
            id, user_id, symbol, side, entry_price, qty, pnl, status,
            reason, sl_price, last_ltp, capital_slot, trading_mode,
            created_at, quote_updated_at, quote_source
        ) VALUES (?, 9, ?, 'CE', ?, ?, 0, 'OPEN', '', ?, ?, ?, 'paper', ?, ?, 'UPSTOX_RUNTIME_LTP')
        """,
        [
            (11, "BANKNIFTY57300CE", 484.30, 120, 470.75, 542.45, 1, now, now),
            (12, "NIFTY24300CE", 62.75, 715, 59.65, 73.30, 2, now, now),
        ],
    )
    conn.commit()
    return conn


def test_trade_live_returns_every_open_position(monkeypatch):
    conn = _open_trade_db()
    monkeypatch.setattr(routes, "get_current_user", lambda _authorization: {"id": 9})
    monkeypatch.setattr(routes, "get_db", lambda: conn)
    monkeypatch.setattr(routes, "backfill_closed_trade_costs", lambda _user_id: None)
    monkeypatch.setattr(
        routes,
        "calculate_row_net_costs",
        lambda row, exit_price: {
            "market_gross_pnl": (exit_price - row["entry_price"]) * row["qty"],
            "total_charges": 0,
            "net_pnl": (exit_price - row["entry_price"]) * row["qty"],
        },
    )

    payload = routes.get_live_trade_price("Bearer test")

    assert payload["open"] is True
    assert payload["open_trade_count"] == 2
    assert [trade["id"] for trade in payload["trades"]] == [11, 12]
    assert payload["trade"]["id"] == 11
    assert payload["trades"][0]["live_price"] == 542.45
    assert payload["trades"][1]["live_price"] == 73.30
    assert payload["all_quotes_fresh"] is True
    assert payload["stale_trade_ids"] == []


def test_open_quote_without_recent_timestamp_is_marked_stale():
    old = (
        datetime.now(timezone.utc) - timedelta(seconds=60)
    ).isoformat().replace("+00:00", "Z")
    metrics = routes._quote_freshness(
        {
            "status": "OPEN",
            "quote_updated_at": old,
            "quote_source": "UPSTOX_RUNTIME_LTP",
        },
        "OPEN",
    )

    assert metrics["quote_stale"] is True
    assert metrics["quote_age_seconds"] >= 59
    assert metrics["quote_source"] == "UPSTOX_RUNTIME_LTP"
