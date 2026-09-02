from datetime import datetime, timedelta, timezone
import sqlite3

from bot.full_sl_velocity_circuit_patch import (
    DAY_STOP_REASON,
    PAUSE_REASON,
    _active_block,
    _register_full_sl,
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _trade(trade_id, mode="paper"):
    return {
        "id": trade_id,
        "underlying": "NIFTY",
        "symbol": "NIFTY TEST CE",
        "side": "CE",
        "trading_mode": mode,
    }


def test_two_full_stops_inside_60m_pause_for_45m():
    conn = _conn()
    start = datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)
    assert _register_full_sl(conn, 1, _trade(1), "PURE ATR SL HIT", start)
    assert _register_full_sl(
        conn, 1, _trade(2), "PURE ATR SL HIT", start + timedelta(minutes=30)
    )

    block = _active_block(
        conn, 1, "paper", start + timedelta(minutes=40)
    )
    assert block["reason"] == PAUSE_REASON
    assert 2099 <= block["remaining_seconds"] <= 2100


def test_third_full_stop_blocks_rest_of_trading_day():
    conn = _conn()
    start = datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)
    for trade_id, minutes in ((1, 0), (2, 70), (3, 140)):
        _register_full_sl(
            conn,
            1,
            _trade(trade_id),
            "PURE ATR SL HIT",
            start + timedelta(minutes=minutes),
        )

    block = _active_block(conn, 1, "paper", start + timedelta(minutes=150))
    assert block["reason"] == DAY_STOP_REASON
    assert block["full_sl_count_today"] == 3


def test_profit_lock_and_live_mode_do_not_count_as_paper_full_stops():
    conn = _conn()
    now = datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)
    assert not _register_full_sl(
        conn, 1, _trade(1), "PROFIT LOCK TRAIL HIT", now
    )
    assert _register_full_sl(
        conn, 1, _trade(2, "live"), "PURE ATR SL HIT", now
    )
    assert _active_block(conn, 1, "paper", now) is None
