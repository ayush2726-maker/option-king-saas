from datetime import datetime, timedelta, timezone
import sqlite3

from bot import auto_portfolio_runtime as runtime
from bot import final_correlated_risk_guard as guard


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _selected(index="BANKNIFTY", side="PE"):
    return {"underlying": index, "signal_data": {"signal": side, "score": 82}}


def _install(monkeypatch, rows):
    monkeypatch.delattr(runtime, "_okai_final_correlated_risk_guard_v1", raising=False)
    monkeypatch.setattr(runtime, "_open_rows", lambda conn, user_id: rows)
    monkeypatch.setattr(runtime, "_open_common", lambda *args, **kwargs: True)
    monkeypatch.setattr(runtime, "_close", lambda *args, **kwargs: None)
    guard.apply_final_correlated_risk_guard()


def _open(conn, state, selected):
    return runtime._open_common(
        conn, 1, "upstox", selected, {"trading_mode": "paper"},
        {"symbol": "TEST"}, 100, {"allowed": True}, 1,
        lambda *args: {}, lambda: 100000, state,
    )


def test_same_direction_correlated_trade_is_allowed(monkeypatch):
    rows = [{
        "id": 7, "underlying": "NIFTY", "side": "PE", "trading_mode": "paper",
        "entry_price": 100, "last_ltp": 95, "qty": 10,
    }]
    _install(monkeypatch, rows)
    state = {}
    assert _open(_conn(), state, _selected("SENSEX", "PE")) is True
    assert "entry_block_reason" not in state


def test_qualified_opposite_trade_is_allowed_only_when_existing_trade_loses(monkeypatch):
    losing = [{
        "id": 8, "underlying": "NIFTY", "side": "PE", "trading_mode": "paper",
        "entry_price": 100, "last_ltp": 90, "qty": 10,
    }]
    _install(monkeypatch, losing)
    assert _open(_conn(), {}, _selected("SENSEX", "CE")) is True

    winning = [{**losing[0], "last_ltp": 120}]
    monkeypatch.setattr(runtime, "_open_rows", lambda conn, user_id: winning)
    state = {}
    assert _open(_conn(), state, _selected("SENSEX", "CE")) is False
    assert state["entry_block_reason"] == guard.HEDGE_NOT_LOSING_REASON

    no_live_quote = [{**losing[0], "last_ltp": None}]
    monkeypatch.setattr(runtime, "_open_rows", lambda conn, user_id: no_live_quote)
    state = {}
    assert _open(_conn(), state, _selected("SENSEX", "CE")) is False
    assert state["entry_guard"]["live_loss_confirmed"] is False


def test_exact_same_index_side_cooldown_is_hard_blocked(monkeypatch):
    _install(monkeypatch, [])
    conn = _conn()
    guard.cooldown._ensure_guard_schema(conn)
    until = datetime.now(timezone.utc) + timedelta(minutes=15)
    conn.execute(
        """
        INSERT INTO auto_reentry_blocks
          (user_id, underlying, side, blocked_until, reason, created_at)
        VALUES (1, 'NIFTY', 'PE', ?, ?, ?)
        """,
        (guard._iso(until), guard.COOLDOWN_REASON, guard._iso(datetime.now(timezone.utc))),
    )
    conn.commit()
    state = {}
    assert _open(conn, state, _selected("NIFTY", "PE")) is False
    assert state["entry_block_reason"] == guard.COOLDOWN_REASON
    assert 890 <= state["entry_guard"]["remaining_seconds"] <= 900


def test_loss_close_persists_new_15_minute_block(monkeypatch):
    _install(monkeypatch, [])
    conn = _conn()
    conn.execute(
        """
        CREATE TABLE paper_trades (
          id INTEGER PRIMARY KEY, user_id INTEGER, underlying TEXT, symbol TEXT,
          side TEXT, entry_price REAL, exit_price REAL, qty INTEGER, pnl REAL,
          net_pnl REAL, status TEXT, reason TEXT
        )
        """
    )
    trade = {
        "id": 2, "user_id": 1, "underlying": "NIFTY", "symbol": "NIFTY TEST PE",
        "side": "PE", "entry_price": 100, "qty": 10,
    }
    conn.execute(
        "INSERT INTO paper_trades VALUES (2,1,'NIFTY','NIFTY TEST PE','PE',100,90,10,-100,-110,'CLOSED','PURE ATR SL HIT')"
    )
    conn.commit()
    runtime._close(conn, 1, trade, 90, "PURE ATR SL HIT")
    row = conn.execute("SELECT * FROM auto_reentry_blocks").fetchone()
    seconds = (guard._parse(row["blocked_until"]) - datetime.now(timezone.utc)).total_seconds()
    assert row["reason"] == guard.COOLDOWN_REASON
    assert row["previous_pnl"] == -110
    assert 895 <= seconds <= 900
