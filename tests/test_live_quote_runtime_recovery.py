import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import auto_portfolio_runtime as runtime
from bot import expiry_hardlock_one_second_monitor_patch as one_second
from bot import live_quote_runtime_recovery as recovery


def _memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            broker_name TEXT,
            last_ltp REAL
        )
        """
    )
    conn.commit()
    return conn


def test_successful_monitor_quote_gets_real_quote_timestamp(monkeypatch):
    conn = _memory_db()
    conn.execute(
        "INSERT INTO paper_trades (id, user_id, status, broker_name) VALUES (7, 1, 'OPEN', 'upstox')"
    )
    conn.commit()

    def base_ensure(_conn):
        return None

    def base_update(_conn, trade, ltp, _evaluation):
        _conn.execute(
            "UPDATE paper_trades SET last_ltp=? WHERE id=?",
            (ltp, trade["id"]),
        )
        _conn.commit()

    monkeypatch.setattr(runtime, "_ensure_schema", base_ensure)
    monkeypatch.setattr(runtime, "_update_open", base_update)
    monkeypatch.setattr(runtime, "_okai_live_quote_timestamp_v1", False, raising=False)
    monkeypatch.setattr(recovery, "_quote_columns_ready", False)

    recovery.apply_live_quote_timestamp_patch()
    runtime._ensure_schema(conn)
    runtime._update_open(
        conn,
        {"id": 7, "broker_name": "upstox"},
        687.50,
        {},
    )

    row = conn.execute(
        "SELECT last_ltp, quote_updated_at, quote_source FROM paper_trades WHERE id=7"
    ).fetchone()
    assert row["last_ltp"] == 687.50
    assert str(row["quote_updated_at"]).endswith("Z")
    assert row["quote_source"] == "UPSTOX_RUNTIME_LTP"


def test_open_user_recovery_starts_only_persisted_running_user(monkeypatch):
    conn = _memory_db()
    conn.execute("CREATE TABLE user_bot_state (user_id INTEGER PRIMARY KEY, is_running INTEGER)")
    conn.execute("INSERT INTO paper_trades (id, user_id, status) VALUES (1, 10, 'OPEN')")
    conn.execute("INSERT INTO user_bot_state (user_id, is_running) VALUES (10, 1)")
    conn.commit()

    monkeypatch.setattr(recovery, "get_db", lambda: conn)
    monkeypatch.setattr(recovery, "_runtime_state", lambda _user_id: {"running": False})
    calls = []
    monkeypatch.setattr(
        recovery,
        "_start_runtime",
        lambda user_id: calls.append(user_id) or {"started": True, "reason": None},
    )

    result = recovery.recover_user_runtime_if_needed(10)
    assert result["attempted"] is True
    assert result["started"] is True
    assert calls == [10]


def test_running_memory_runtime_is_not_started_twice(monkeypatch):
    conn = _memory_db()
    conn.execute("CREATE TABLE user_bot_state (user_id INTEGER PRIMARY KEY, is_running INTEGER)")
    conn.execute("INSERT INTO user_bot_state (user_id, is_running) VALUES (4, 1)")
    conn.commit()

    monkeypatch.setattr(recovery, "_runtime_state", lambda _user_id: {"running": True})
    monkeypatch.setattr(recovery, "get_db", lambda: conn)
    monkeypatch.setattr(
        recovery,
        "_start_runtime",
        lambda _user_id: (_ for _ in ()).throw(AssertionError("must not start twice")),
    )

    result = recovery.recover_user_runtime_if_needed(4)
    assert result["already_running"] is True
    assert result["attempted"] is False


def test_all_stale_quotes_restart_running_runtime_once(monkeypatch):
    conn = _memory_db()
    conn.execute("CREATE TABLE user_bot_state (user_id INTEGER PRIMARY KEY, is_running INTEGER)")
    conn.execute("INSERT INTO user_bot_state (user_id, is_running) VALUES (12, 1)")
    conn.execute(
        "INSERT INTO paper_trades (id, user_id, status) VALUES (21, 12, 'OPEN')"
    )
    conn.commit()

    monkeypatch.setattr(recovery, "_quote_columns_ready", False)
    monkeypatch.setattr(recovery, "_runtime_state", lambda _user_id: {"running": True})
    monkeypatch.setattr(recovery, "get_db", lambda: conn)
    monkeypatch.setattr(recovery, "_last_restart_attempt", {})
    calls = []
    monkeypatch.setattr(
        recovery,
        "_restart_stale_runtime",
        lambda user_id: calls.append(user_id)
        or {"attempted": True, "started": True, "stale_runtime_restarted": True},
    )

    result = recovery.recover_user_runtime_if_needed(12)

    assert result["all_stale"] is True
    assert result["stale_runtime_restarted"] is True
    assert calls == [12]


def test_one_fresh_open_quote_does_not_restart_runtime(monkeypatch):
    conn = _memory_db()
    conn.execute("CREATE TABLE user_bot_state (user_id INTEGER PRIMARY KEY, is_running INTEGER)")
    conn.execute("INSERT INTO user_bot_state (user_id, is_running) VALUES (13, 1)")
    conn.execute(
        "INSERT INTO paper_trades (id, user_id, status) VALUES (22, 13, 'OPEN')"
    )
    monkeypatch.setattr(recovery, "_quote_columns_ready", False)
    recovery._ensure_quote_columns(conn)
    conn.execute(
        "UPDATE paper_trades SET quote_updated_at=? WHERE id=22",
        (recovery._utc_now(),),
    )
    conn.commit()

    monkeypatch.setattr(recovery, "_runtime_state", lambda _user_id: {"running": True})
    monkeypatch.setattr(recovery, "get_db", lambda: conn)
    monkeypatch.setattr(
        recovery,
        "_restart_stale_runtime",
        lambda _user_id: (_ for _ in ()).throw(AssertionError("fresh runtime must not restart")),
    )

    result = recovery.recover_user_runtime_if_needed(13)

    assert result["all_stale"] is False
    assert result["already_running"] is True
    assert result["attempted"] is False


def test_trade_live_response_forbids_http_cache():
    app = FastAPI()
    app.add_middleware(recovery.TradeLiveRuntimeRecoveryMiddleware)

    @app.get("/bot/trade-live")
    def trade_live():
        return {"success": True}

    with TestClient(app) as client:
        response = client.get("/bot/trade-live")

    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-okai-live-quote-version"] == recovery.VERSION


def test_repeated_quote_failures_are_visible_and_request_fast_reconnect(monkeypatch):
    conn = _memory_db()
    conn.execute(
        "INSERT INTO paper_trades (id, user_id, status, broker_name) "
        "VALUES (31, 7, 'OPEN', 'upstox')"
    )
    conn.commit()
    monkeypatch.setattr(recovery, "_quote_columns_ready", False)
    recovery._ensure_quote_columns(conn)

    state = {}
    trade = conn.execute("SELECT * FROM paper_trades WHERE id=31").fetchone()
    for _ in range(3):
        runtime._manage_rows(
            conn,
            7,
            [trade],
            [],
            lambda _trade: {
                "success": False,
                "message": "UPSTOX_LTP_V3:401:invalid token",
            },
            lambda *_args: {"success": False},
            state,
        )

    row = conn.execute(
        "SELECT quote_error, quote_failure_count, quote_failed_at "
        "FROM paper_trades WHERE id=31"
    ).fetchone()
    assert "invalid token" in row["quote_error"]
    assert row["quote_failure_count"] == 3
    assert row["quote_failed_at"]
    assert state["consecutive_open_quote_failures"] == 3

    try:
        one_second._raise_after_repeated_quote_failures(
            state, "upstox", [trade]
        )
    except one_second.QuoteFeedStaleError as exc:
        assert "UPSTOX_OPEN_QUOTE_FAILED_3X" in str(exc)
    else:
        raise AssertionError("three failed quote cycles must reset the session")
