import sqlite3

from bot import auto_portfolio_runtime as runtime


def _conn_with_trades(count=0):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE paper_trades "
        "(id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT, created_at TEXT)"
    )
    for _ in range(count):
        conn.execute(
            "INSERT INTO paper_trades "
            "(user_id, status, created_at) "
            "VALUES (1, 'CLOSED', datetime('now'))"
        )
    conn.commit()
    return conn


def test_paper_mode_is_unlimited_by_default_for_saas_testing(monkeypatch):
    conn = _conn_with_trades(8)
    monkeypatch.setattr(runtime, "_today_count", lambda conn, user_id: 8)
    state = {"live_order_lock": True}
    allowed = runtime._can_enter(
        conn,
        1,
        {"trading_mode": "paper", "max_trades_per_day": 5},
        [],
        state,
    )
    assert allowed is True
    assert state["entry_permission"]["unlimited"] is True
    assert "live_order_lock" not in state


def test_live_mode_keeps_daily_limit(monkeypatch):
    conn = _conn_with_trades(5)
    monkeypatch.setattr(runtime, "_today_count", lambda conn, user_id: 5)
    state = {}
    allowed = runtime._can_enter(
        conn,
        1,
        {"trading_mode": "live", "max_trades_per_day": 5},
        [],
        state,
    )
    assert allowed is False
    assert state["entry_permission"]["reason"] == "DAILY_TRADE_LIMIT_REACHED"


def test_explicit_paper_limit_can_still_be_enabled(monkeypatch):
    conn = _conn_with_trades(2)
    monkeypatch.setattr(runtime, "_today_count", lambda conn, user_id: 2)
    state = {}
    allowed = runtime._can_enter(
        conn,
        1,
        {
            "trading_mode": "paper",
            "max_trades_per_day": 2,
            "unlimited_trades": False,
            "paper_unlimited_trades": False,
        },
        [],
        state,
    )
    assert allowed is False
    assert state["entry_permission"]["reason"] == "DAILY_TRADE_LIMIT_REACHED"


def test_preopen_failure_is_visible():
    state = {}
    selected = {
        "underlying": "BANKNIFTY",
        "signal_data": {"signal": "PE", "score": 90},
    }
    result = runtime._record_preopen_failure(
        state,
        "upstox",
        selected,
        "Upstox option not found",
        "OPTION_CONTRACT",
    )
    assert result is False
    assert state["last_entry_attempt"]["reason"] == "Upstox option not found"
    assert state["last_entry_attempt"]["stage"] == "OPTION_CONTRACT"
    assert state["last_entry_attempt"]["underlying"] == "BANKNIFTY"
