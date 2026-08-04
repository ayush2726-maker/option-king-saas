import os
import sqlite3

from bot import admin_morning_trade_cleanup_20260804 as cleanup


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _prepare(path):
    conn = _connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT,
            is_admin INTEGER DEFAULT 0
        );
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            symbol TEXT,
            trading_mode TEXT DEFAULT 'paper',
            status TEXT,
            entry_time TEXT,
            created_at TEXT,
            pnl REAL,
            net_pnl REAL
        );
        CREATE TABLE bot_status (
            user_id INTEGER PRIMARY KEY,
            total_trades INTEGER,
            total_pnl REAL,
            updated_at TEXT
        );
        CREATE TABLE auto_reentry_blocks (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            source_trade_id INTEGER
        );
        CREATE TABLE auto_user_cooldowns (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            source_trade_id INTEGER
        );
        CREATE TABLE live_order_events (
            id INTEGER PRIMARY KEY,
            trade_id INTEGER
        );
        """
    )
    conn.executemany(
        "INSERT INTO users (id, email, is_admin) VALUES (?, ?, ?)",
        [(1, "admin@example.com", 1), (2, "other@example.com", 0)],
    )
    # UTC timestamps: 03:45 = 09:15 IST, 04:08:59 = 09:38:59 IST.
    rows = [
        (1, 1, "NIFTY A", "paper", "CLOSED", "2026-08-04T03:44:59Z", None, 10, 10),
        (2, 1, "NIFTY B", "paper", "CLOSED", "2026-08-04T03:45:00Z", None, -100, -100),
        (3, 1, "SENSEX C", "paper", "CLOSED", "2026-08-04T04:08:59Z", None, -200, -200),
        (4, 1, "BANKNIFTY D", "paper", "CLOSED", "2026-08-04T04:09:00Z", None, 40, 40),
        (5, 1, "BANKNIFTY LIVE", "live", "CLOSED", "2026-08-04T03:50:00Z", None, 50, 50),
        (6, 2, "OTHER USER", "paper", "CLOSED", "2026-08-04T03:50:00Z", None, 60, 60),
        (7, 1, "NEXT DAY", "paper", "CLOSED", "2026-08-05T03:50:00Z", None, 70, 70),
    ]
    conn.executemany(
        """
        INSERT INTO paper_trades (
            id, user_id, symbol, trading_mode, status,
            entry_time, created_at, pnl, net_pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.execute(
        "INSERT INTO bot_status (user_id, total_trades, total_pnl) VALUES (1, 999, 999999)"
    )
    conn.executemany(
        "INSERT INTO auto_reentry_blocks (id, user_id, source_trade_id) VALUES (?, ?, ?)",
        [(1, 1, 2), (2, 1, 999)],
    )
    conn.executemany(
        "INSERT INTO auto_user_cooldowns (id, user_id, source_trade_id) VALUES (?, ?, ?)",
        [(1, 1, 3), (2, 1, 999)],
    )
    conn.execute("INSERT INTO live_order_events (id, trade_id) VALUES (1, 2)")
    conn.commit()
    conn.close()


def test_permanently_deletes_only_exact_admin_paper_window(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cleanup.sqlite")
    _prepare(db_path)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(cleanup, "get_db", lambda: _connect(db_path))

    result = cleanup.delete_admin_morning_paper_trades_20260804()

    assert result["removed"] == 2
    assert result["removed_trade_ids"] == [2, 3]
    assert result["affected_users"] == 1
    assert result["removed_recorded_pnl"] == -300.0

    conn = _connect(db_path)
    remaining_ids = [
        row[0]
        for row in conn.execute("SELECT id FROM paper_trades ORDER BY id").fetchall()
    ]
    assert remaining_ids == [1, 4, 5, 6, 7]

    # History count and cumulative closed P&L are rebuilt from remaining rows.
    status = conn.execute(
        "SELECT total_trades, total_pnl FROM bot_status WHERE user_id=1"
    ).fetchone()
    assert status["total_trades"] == 4
    assert status["total_pnl"] == 170.0

    # Deleted-loss cooldown/re-entry state cannot remain active.
    assert conn.execute(
        "SELECT COUNT(*) FROM auto_reentry_blocks WHERE user_id=1"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM auto_user_cooldowns WHERE user_id=1"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM live_order_events WHERE trade_id IN (2, 3)"
    ).fetchone()[0] == 0

    run = conn.execute(
        "SELECT removed_rows FROM maintenance_run_log WHERE version=?",
        (cleanup.VERSION,),
    ).fetchone()
    assert run["removed_rows"] == 2
    conn.close()


def test_cleanup_is_one_time_and_does_not_touch_remaining_rows(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cleanup.sqlite")
    _prepare(db_path)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(cleanup, "get_db", lambda: _connect(db_path))

    first = cleanup.delete_admin_morning_paper_trades_20260804()
    second = cleanup.delete_admin_morning_paper_trades_20260804()

    assert first["removed"] == 2
    assert second["already_applied"] is True
    assert second["removed"] == 0

    conn = _connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 5
    conn.close()


def test_naive_database_timestamp_is_treated_as_utc():
    row = {
        "trading_mode": "paper",
        "entry_time": "2026-08-04 03:45:00",
    }
    assert cleanup._matches_target(row) is True


def test_exact_0939_ist_is_not_deleted():
    row = {
        "trading_mode": "paper",
        "entry_time": "2026-08-04T04:09:00Z",
    }
    assert cleanup._matches_target(row) is False
