import sqlite3
from datetime import datetime, timezone

from bot.index_report_card import build_index_report_card


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            symbol TEXT,
            underlying TEXT,
            trading_mode TEXT,
            status TEXT,
            pnl REAL,
            net_pnl REAL
        )
        """
    )
    return conn


def test_all_time_index_report_uses_closed_net_pnl_and_ranks_best():
    conn = _db()
    rows = [
        (1, 7, "NIFTY26CE", "NIFTY", "paper", "CLOSED", 120, 100),
        (2, 7, "NIFTY26PE", "NIFTY", "paper", "CLOSED", -35, -50),
        (3, 7, "BANKNIFTY26CE", None, "paper", "CLOSED", -200, -220),
        (4, 7, "SENSEX26PE", None, "live", "CLOSED", 90, 80),
        (5, 7, "NIFTY26CE", "NIFTY", "paper", "OPEN", 999, 999),
        (6, 7, "UNKNOWN", None, "paper", "CLOSED", 50, 50),
        (7, 9, "NIFTY26CE", "NIFTY", "paper", "CLOSED", 9999, 9999),
    ]
    conn.executemany(
        "INSERT INTO paper_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )

    report = build_index_report_card(
        conn,
        7,
        now=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    by_index = {row["instrument"]: row for row in report["indices"]}

    assert report["total_trades"] == 5
    assert report["closed_trades"] == 4
    assert report["open_trades"] == 1
    assert report["unclassified_trades"] == 1
    assert report["total_realized_pnl"] == -90
    assert report["best_index"] == "SENSEX"

    assert by_index["NIFTY"]["total_trades"] == 3
    assert by_index["NIFTY"]["closed_trades"] == 2
    assert by_index["NIFTY"]["open_trades"] == 1
    assert by_index["NIFTY"]["realized_pnl"] == 50
    assert by_index["NIFTY"]["win_rate"] == 50
    assert by_index["NIFTY"]["average_pnl"] == 25
    assert by_index["NIFTY"]["best_trade"] == 100
    assert by_index["NIFTY"]["worst_trade"] == -50
    assert by_index["NIFTY"]["profit_factor"] == 2

    assert by_index["BANKNIFTY"]["realized_pnl"] == -220
    assert by_index["SENSEX"]["realized_pnl"] == 80
    conn.close()


def test_mode_filter_and_invalid_mode_fallback():
    conn = _db()
    conn.executemany(
        "INSERT INTO paper_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 7, "NIFTY26CE", "NIFTY", "paper", "CLOSED", 10, 10),
            (2, 7, "NIFTY26PE", "NIFTY", "live", "CLOSED", 25, 25),
        ],
    )

    live = build_index_report_card(conn, 7, mode="live")
    assert live["mode"] == "live"
    assert live["total_trades"] == 1
    assert live["total_realized_pnl"] == 25

    fallback = build_index_report_card(conn, 7, mode="invalid")
    assert fallback["mode"] == "all"
    assert fallback["total_trades"] == 2
    assert fallback["total_realized_pnl"] == 35
    conn.close()
