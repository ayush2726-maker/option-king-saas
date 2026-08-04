from datetime import datetime, timezone
import sqlite3

from bot.opening_orb_loss_circuit_patch import (
    GLOBAL_BLOCK_REASON,
    SAME_SIDE_BLOCK_REASON,
    _global_block,
    _opening_ready,
    _record_close_outcome,
    _same_side_block,
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _trade(trade_id, underlying, side, entry=100.0, qty=10):
    return {
        "id": trade_id,
        "underlying": underlying,
        "symbol": f"{underlying} TEST {side}",
        "side": side,
        "entry_price": entry,
        "qty": qty,
    }


def test_normal_auto_waits_for_completed_opening_range():
    before = datetime(2026, 8, 4, 9, 29, tzinfo=timezone.utc)
    ready = datetime(2026, 8, 4, 9, 30, tzinfo=timezone.utc)

    assert _opening_ready(before) is False
    assert _opening_ready(ready) is True


def test_losing_close_blocks_same_index_and_side_for_15_minutes():
    conn = _conn()
    trade = _trade(1, "NIFTY", "PE", entry=30.0, qty=100)

    outcome = _record_close_outcome(
        conn,
        7,
        trade,
        27.0,
        "PURE ATR SL HIT",
    )
    block = _same_side_block(conn, 7, "NIFTY", "PE")

    assert outcome["loss"] is True
    assert outcome["same_side_blocked"] is True
    assert block is not None
    assert block["reason"] == SAME_SIDE_BLOCK_REASON
    assert block["previous_pnl"] == -300.0
    assert _same_side_block(conn, 7, "NIFTY", "CE") is None


def test_two_consecutive_losses_activate_global_cooldown():
    conn = _conn()

    first = _record_close_outcome(
        conn,
        9,
        _trade(11, "NIFTY", "PE"),
        95.0,
        "PURE ATR SL HIT",
    )
    assert first["consecutive_losses"] == 1
    assert _global_block(conn, 9) is None

    second = _record_close_outcome(
        conn,
        9,
        _trade(12, "SENSEX", "PE"),
        94.0,
        "PURE ATR SL HIT",
    )
    block = _global_block(conn, 9)

    assert second["consecutive_losses"] == 2
    assert second["global_blocked"] is True
    assert block is not None
    assert block["reason"] == GLOBAL_BLOCK_REASON
    assert block["consecutive_losses"] == 2


def test_profitable_close_resets_consecutive_loss_count():
    conn = _conn()
    _record_close_outcome(
        conn,
        10,
        _trade(21, "NIFTY", "PE"),
        95.0,
        "PURE ATR SL HIT",
    )
    _record_close_outcome(
        conn,
        10,
        _trade(22, "SENSEX", "PE"),
        94.0,
        "PURE ATR SL HIT",
    )
    assert _global_block(conn, 10) is not None

    outcome = _record_close_outcome(
        conn,
        10,
        _trade(23, "BANKNIFTY", "CE"),
        103.0,
        "PROFIT LOCK TRAIL HIT",
    )

    assert outcome["loss"] is False
    assert outcome["consecutive_losses"] == 0
    assert _global_block(conn, 10) is None
