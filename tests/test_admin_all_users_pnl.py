import sqlite3
from datetime import datetime, timezone

from admin.pnl_report import build_all_user_pnl_report


def _costs(row, exit_price=None):
    entry = float(row["entry_price"] or 0)
    qty = int(row["qty"] or 0)
    exit_value = float(exit_price if exit_price is not None else row["exit_price"] or entry)
    gross = (exit_value - entry) * qty
    return {"net_pnl": gross - 10, "total_charges": 10}


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, name TEXT, email TEXT,
            subscription_status TEXT, is_active INTEGER
        );
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY, user_id INTEGER, entry_price REAL,
            exit_price REAL, qty INTEGER, pnl REAL, net_pnl REAL,
            status TEXT, trading_mode TEXT, last_ltp REAL,
            broker_name TEXT, underlying TEXT, created_at TEXT
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, user_id INTEGER, quantity INTEGER,
            pnl REAL, status TEXT, created_at TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO users VALUES (?,?,?,?,?)",
        [
            (1, "Ayush", "ayush@example.com", "active", 1),
            (2, "Demo", "demo@example.com", "trial", 1),
        ],
    )
    return conn


def test_report_separates_today_all_time_paper_live_and_users():
    conn = _db()
    conn.executemany(
        """
        INSERT INTO paper_trades
        (id,user_id,entry_price,exit_price,qty,pnl,net_pnl,status,trading_mode,
         last_ltp,broker_name,underlying,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (1, 1, 100, 110, 10, 100, 90, "CLOSED", "paper", 110, "upstox", "NIFTY", "2026-08-17T04:00:00+00:00"),
            (2, 1, 200, 190, 10, -100, -115, "CLOSED", "live", 190, "upstox", "BANKNIFTY", "2026-08-16T04:00:00+00:00"),
            (3, 2, 50, None, 10, 0, None, "OPEN", "paper", 60, "upstox", "NIFTY", "2026-08-17T05:00:00+00:00"),
        ],
    )
    conn.execute(
        "INSERT INTO trades VALUES (1,1,10,50,'closed','2026-08-17T06:00:00+00:00')"
    )

    report = build_all_user_pnl_report(
        conn,
        now=datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc),
        cost_calculator=_costs,
    )

    assert report["user_count"] == 2
    ayush, demo = report["users"]
    assert ayush["paper"]["today"]["net_pnl"] == 90
    assert ayush["live"]["today"]["net_pnl"] == 50
    assert ayush["all_time_net_pnl"] == 25
    assert demo["paper"]["all_time"]["open_trades"] == 1
    assert demo["paper"]["all_time"]["open_pnl"] < 100
    assert report["totals"]["combined"]["today"]["net_pnl"] == round(
        140 + demo["today_net_pnl"], 2
    )


def test_unpriced_open_gateway_trade_is_not_reported_as_zero_profit():
    conn = _db()
    conn.execute(
        "INSERT INTO trades VALUES (1,1,10,NULL,'open','2026-08-17T06:00:00+00:00')"
    )

    report = build_all_user_pnl_report(
        conn,
        now=datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc),
        cost_calculator=_costs,
    )
    live = report["users"][0]["live"]["all_time"]
    assert live["open_trades"] == 1
    assert live["unpriced_open_trades"] == 1
    assert live["open_pnl"] == 0


def test_report_does_not_change_trade_rows():
    conn = _db()
    conn.execute(
        """
        INSERT INTO paper_trades
        VALUES (1,1,100,110,10,100,NULL,'CLOSED','paper',110,'upstox','NIFTY',
                '2026-08-17T04:00:00+00:00')
        """
    )
    before = dict(conn.execute("SELECT * FROM paper_trades WHERE id=1").fetchone())
    build_all_user_pnl_report(
        conn,
        now=datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc),
        cost_calculator=_costs,
    )
    after = dict(conn.execute("SELECT * FROM paper_trades WHERE id=1").fetchone())
    assert after == before
