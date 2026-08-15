import sqlite3

from bot.routes import _status_capital_snapshot


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
            created_at TEXT
        )
        """
    )
    return conn


def test_paper_current_capital_uses_carry_forward_and_open_pnl():
    conn = _conn()
    conn.execute(
        """
        INSERT INTO paper_trades
            (user_id, status, pnl, net_pnl, trading_mode, created_at)
        VALUES (1, 'CLOSED', 9146.54, 9146.54, 'paper', datetime('now'))
        """
    )
    conn.execute(
        """
        INSERT INTO paper_trades
            (user_id, status, pnl, net_pnl, trading_mode, created_at)
        VALUES (1, 'CLOSED', 50000, 50000, 'live', datetime('now'))
        """
    )
    conn.commit()

    result = _status_capital_snapshot(
        conn,
        1,
        {"paper_capital": 120000},
        "paper",
        120000,
        59146.54,
        [{"unrealized_pnl": 500}],
    )

    assert result["starting_capital"] == 120000
    assert result["current_capital"] == 129646.54
    assert result["open_pnl"] == 500
    assert result["capital_source"] == "PAPER_CARRY_FORWARD_PLUS_OPEN_PNL"
    conn.close()


def test_live_current_capital_never_uses_paper_seed():
    conn = _conn()
    result = _status_capital_snapshot(
        conn,
        1,
        {"paper_capital": 500000},
        "live",
        500000,
        0,
        [{"capital_base": 120000, "unrealized_pnl": -200}],
    )

    assert result["starting_capital"] == 120000
    assert result["current_capital"] == 119800
    assert result["capital_source"] == "LIVE_BROKER_BASE_PLUS_OPEN_PNL"
    conn.close()
