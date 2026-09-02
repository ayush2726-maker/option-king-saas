import sqlite3

from bot.capital_based_sizing_restore_patch import (
    _configured_paper_sizing_base,
    _runtime_capital_size,
)
from bot.authoritative_ledger import build_authoritative_ledger


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            status TEXT,
            pnl REAL,
            net_pnl REAL,
            trading_mode TEXT,
            broker_name TEXT,
            entry_order_id TEXT,
            entry_price REAL,
            last_ltp REAL,
            qty INTEGER,
            capital_base REAL,
            created_at TEXT,
            exit_time TEXT
        )
        """
    )
    return conn


def _closed(conn, pnl, created_at):
    conn.execute(
        """
        INSERT INTO paper_trades(
            user_id, status, pnl, net_pnl, trading_mode,
            entry_price, last_ltp, qty, created_at, exit_time
        ) VALUES (1, 'CLOSED', ?, ?, 'paper', 100, 100, 1, ?, ?)
        """,
        (pnl, pnl, created_at, created_at),
    )
    conn.commit()


def test_profit_after_reset_increases_next_paper_sizing_base():
    conn = _conn(); _closed(conn, 5000, "2026-09-02 10:00:00")
    settings = {"trading_mode": "paper", "paper_capital": 20000, "paper_capital_reset_at": "2026-09-02 09:00:00"}
    assert _configured_paper_sizing_base(conn, 1, settings) == 25000
    ledger = build_authoritative_ledger(conn, 1, settings)
    assert ledger["starting_capital"] == 20000
    assert ledger["current_capital"] == 25000
    conn.close()


def test_loss_after_reset_reduces_next_paper_sizing_base():
    conn = _conn(); _closed(conn, -5000, "2026-09-02 10:00:00")
    settings = {"trading_mode": "paper", "paper_capital": 20000, "paper_capital_reset_at": "2026-09-02 09:00:00"}
    assert _configured_paper_sizing_base(conn, 1, settings) == 15000
    conn.close()


def test_old_profit_is_ignored_after_new_capital_reset():
    conn = _conn(); _closed(conn, 30000, "2026-09-01 10:00:00"); _closed(conn, 5000, "2026-09-02 10:00:00")
    settings = {"trading_mode": "paper", "paper_capital": 20000, "paper_capital_reset_at": "2026-09-02 09:00:00"}
    assert _configured_paper_sizing_base(conn, 1, settings) == 25000
    conn.close()


def test_runtime_quantity_uses_cycle_equity():
    base = _runtime_capital_size(20000, slot=1, premium=90, lot_size=65, rows=[], risk_points=None)
    after_profit = _runtime_capital_size(25000, slot=1, premium=90, lot_size=65, rows=[], risk_points=None)
    assert base["lots"] == 1 and base["qty"] == 65
    assert after_profit["lots"] == 2 and after_profit["qty"] == 130


def test_live_current_capital_uses_fresh_gateway_funds_when_flat():
    conn = _conn()
    conn.execute("CREATE TABLE live_broker_funds (user_id INTEGER PRIMARY KEY, available_cash REAL, used_margin REAL, total_limit REAL, broker TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO live_broker_funds (user_id, available_cash, used_margin, total_limit, broker, updated_at) VALUES (1, 18000, 2000, 20000, 'angelone', datetime('now'))")
    conn.commit()
    ledger = build_authoritative_ledger(conn, 1, {"trading_mode": "live", "paper_capital": 999999})
    assert ledger["starting_capital"] == 20000
    assert ledger["current_capital"] == 20000
    assert ledger["broker_available_cash"] == 18000
    assert ledger["broker_total_limit"] == 20000
    conn.close()
