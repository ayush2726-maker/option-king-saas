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
    assert ledger["source"] == "PAPER_TRADES_DB_NET_PNL_WITH_RECONCILED_BROKER_PROOF"


def test_same_symbol_in_broker_table_does_not_hide_paper_pnl():
    conn = _conn()
    conn.execute("ALTER TABLE paper_trades ADD COLUMN symbol TEXT")
    conn.execute("ALTER TABLE paper_trades ADD COLUMN entry_order_id TEXT")
    conn.execute(
        """
        CREATE TABLE trades (
          id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT,
          broker_order_id TEXT, entry_price REAL, quantity INTEGER
        )
        """
    )
    now = datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc)
    conn.execute(
        """
        INSERT INTO paper_trades
          (id,user_id,symbol,status,pnl,net_pnl,trading_mode,entry_order_id,
           created_at,exit_time,updated_at,entry_price,last_ltp,qty)
        VALUES (1,1,'NIFTY01SEP2625000CE','CLOSED',1200,1100,'paper','',
                '2026-09-01T05:00:00Z','2026-09-01T05:30:00Z',
                '2026-09-01T05:30:00Z',100,120,65)
        """
    )
    conn.execute(
        """
        INSERT INTO trades
          (id,user_id,symbol,broker_order_id,entry_price,quantity)
        VALUES (7,1,'NIFTY01SEP2625000CE','ANGEL-OLD-7',95,65)
        """
    )
    conn.commit()

    ledger = build_authoritative_ledger(
        conn,
        1,
        {"trading_mode": "paper", "paper_capital": 120000},
        now=now,
    )

    assert ledger["today"]["trades"] == 1
    assert ledger["today"]["closed_pnl"] == 1100
    assert ledger["total_trades"] == 1
    assert ledger["realized_pnl"] == 1100
    assert ledger["current_capital"] == 121100


def test_entry_order_id_is_still_strict_live_proof():
    conn = _conn()
    conn.execute("ALTER TABLE paper_trades ADD COLUMN symbol TEXT")
    conn.execute("ALTER TABLE paper_trades ADD COLUMN entry_order_id TEXT")
    now = datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc)
    conn.execute(
        """
        INSERT INTO paper_trades
          (id,user_id,symbol,status,pnl,net_pnl,trading_mode,entry_order_id,
           created_at,exit_time,updated_at,entry_price,last_ltp,qty,capital_base)
        VALUES (1,1,'SENSEX01SEP2680000PE','CLOSED',500,450,'paper','ANGEL-1',
                '2026-09-01T05:00:00Z','2026-09-01T05:30:00Z',
                '2026-09-01T05:30:00Z',100,125,20,120000)
        """
    )
    conn.commit()

    paper = build_authoritative_ledger(
        conn, 1, {"trading_mode": "paper", "paper_capital": 120000}, now=now
    )
    live = build_authoritative_ledger(
        conn, 1, {"trading_mode": "live"}, now=now
    )

    assert paper["total_trades"] == 0
    assert paper["realized_pnl"] == 0
    assert live["total_trades"] == 1
    assert live["realized_pnl"] == 450


def test_wrong_saved_live_flag_without_broker_proof_is_repaired_to_paper():
    conn = _conn()
    conn.execute("ALTER TABLE paper_trades ADD COLUMN symbol TEXT")
    conn.execute("ALTER TABLE paper_trades ADD COLUMN entry_order_id TEXT")
    now = datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc)
    conn.execute(
        """
        INSERT INTO paper_trades
          (id,user_id,symbol,status,pnl,net_pnl,trading_mode,entry_order_id,
           created_at,exit_time,updated_at,entry_price,last_ltp,qty)
        VALUES (1,1,'NIFTY01SEP2625000CE','CLOSED',1200,1100,'live','',
                '2026-09-01T05:00:00Z','2026-09-01T05:30:00Z',
                '2026-09-01T05:30:00Z',100,120,65)
        """
    )
    conn.commit()

    ledger = build_authoritative_ledger(
        conn, 1, {"trading_mode": "paper", "paper_capital": 120000}, now=now
    )

    saved = conn.execute("SELECT trading_mode FROM paper_trades WHERE id=1").fetchone()
    assert saved["trading_mode"] == "paper"
    assert ledger["today"]["trades"] == 1
    assert ledger["today"]["closed_pnl"] == 1100
