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


def test_paper_profit_changes_current_capital_but_not_sizing_capital():
    conn = _conn()
    conn.execute(
        """
        INSERT INTO paper_trades(
            user_id, status, pnl, net_pnl, trading_mode,
            entry_price, last_ltp, qty, created_at, exit_time
        ) VALUES (1, 'CLOSED', 5000, 5000, 'paper', 100, 100, 1,
                  datetime('now'), datetime('now'))
        """
    )
    conn.commit()

    settings = {"trading_mode": "paper", "paper_capital": 20000}
    ledger = build_authoritative_ledger(conn, 1, settings)

    assert ledger["starting_capital"] == 20000
    assert ledger["current_capital"] == 25000
    assert _configured_paper_sizing_base(conn, 1, settings) == 20000
    conn.close()


def test_runtime_quantity_uses_20000_not_25000_after_profit():
    # NIFTY lot 65 at premium 150 costs 9750 per lot.
    # Slot 1 of fixed 20k = 10k, so exactly one lot is affordable.
    fixed = _runtime_capital_size(
        20000,
        slot=1,
        premium=150,
        lot_size=65,
        rows=[],
        risk_points=None,
    )
    compounded = _runtime_capital_size(
        40000,
        slot=1,
        premium=150,
        lot_size=65,
        rows=[],
        risk_points=None,
    )

    assert fixed["capital_base"] == 20000
    assert fixed["lots"] == 1
    assert fixed["qty"] == 65
    assert compounded["lots"] == 2
    assert compounded["qty"] == 130
