import sqlite3

from bot.trade_mode_truth import broker_proof_sql, reconcile_trade_modes


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE paper_trades (
        id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT, entry_price REAL,
        qty INTEGER, entry_time TEXT, entry_order_id TEXT, trading_mode TEXT)"""
    )
    conn.execute(
        """CREATE TABLE trades (
        id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT, entry_price REAL,
        quantity INTEGER, entry_time TEXT, broker_order_id TEXT)"""
    )
    return conn


def test_same_symbol_alone_is_not_live_proof_but_exact_fill_is():
    conn = _conn()
    conn.executemany(
        "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, 1, "NIFTY-X", 100, 65, "2026-09-01T05:00:00Z", "", "live"),
            (2, 1, "NIFTY-X", 105, 65, "2026-09-01T06:00:00Z", "", "paper"),
        ],
    )
    conn.execute(
        "INSERT INTO trades VALUES (7,1,'NIFTY-X',105,65,'2026-09-01T06:02:00Z','A-7')"
    )
    conn.commit()

    repaired = reconcile_trade_modes(conn, 1)
    rows = conn.execute("SELECT id,trading_mode FROM paper_trades ORDER BY id").fetchall()

    assert repaired == 2
    assert [(row["id"], row["trading_mode"]) for row in rows] == [(1, "paper"), (2, "live")]
    proof = broker_proof_sql(conn, "paper_trades")
    assert conn.execute(f"SELECT COUNT(*) AS c FROM paper_trades WHERE {proof}").fetchone()["c"] == 1
