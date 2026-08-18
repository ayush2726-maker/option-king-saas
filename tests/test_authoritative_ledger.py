from datetime import datetime, timedelta, timezone
import sqlite3

from bot.authoritative_ledger import build_authoritative_ledger


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE paper_trades (
          id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT,
          pnl REAL, net_pnl REAL, trading_mode TEXT,
          created_at TEXT, exit_time TEXT, updated_at TEXT,
          entry_price REAL, last_ltp REAL, qty INTEGER, capital_base REAL
        )
        """
    )
    return conn


def test_today_total_and_capital_share_one_net_ledger():
    conn = _conn()
    now = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)
    conn.executemany(
        """
        INSERT INTO paper_trades
          (id,user_id,status,pnl,net_pnl,trading_mode,created_at,exit_time,updated_at,entry_price,last_ltp,qty)
        VALUES (?,?,?,?,?,'paper',?,?,?,?,?,?)
        """,
        [
            (1, 1, "CLOSED", -5000, -5200, "2026-08-18T05:00:00Z", "2026-08-18T05:05:00Z", "2026-08-18T05:05:00Z", 100, 80, 10),
            (2, 1, "CLOSED", 3000, 2800, "2026-08-18T06:00:00Z", "2026-08-18T06:05:00Z", "2026-08-18T06:05:00Z", 100, 120, 10),
            (3, 1, "CLOSED", 99999, 99999, "2026-08-17T05:00:00Z", "2026-08-17T05:05:00Z", "2026-08-17T05:05:00Z", 100, 120, 10),
        ],
    )
    conn.commit()
    ledger = build_authoritative_ledger(
        conn, 1,
        {"trading_mode": "paper", "paper_capital": 120000, "paper_capital_reset_at": "2026-08-18T00:00:00Z"},
        now=now,
    )
    assert ledger["realized_pnl"] == -2400
    assert ledger["today"]["closed_pnl"] == -2400
    assert ledger["today"]["trades"] == 2
    assert ledger["total_trades"] == 2
    assert ledger["current_capital"] == 117600
    assert ledger["source"] == "PAPER_TRADES_DB_NET_PNL"

