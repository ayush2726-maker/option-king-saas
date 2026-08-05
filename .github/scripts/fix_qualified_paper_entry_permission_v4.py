from pathlib import Path

path = Path("bot/auto_portfolio_runtime.py")
text = path.read_text(encoding="utf-8")

old_can_enter = '''def _can_enter(conn, user_id, settings, rows, state):
    if len(rows) >= MAX_OPEN_POSITIONS or state.get("live_order_lock"):
        return False

    max_daily = max(1, _i(settings.get("max_trades_per_day", 5), 5))
    return _today_count(conn, user_id) < max_daily
'''
new_can_enter = '''def _setting_truthy(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {
        "1", "true", "yes", "on", "enabled", "unlimited"
    }


def _can_enter(conn, user_id, settings, rows, state):
    current_mode = (
        "live"
        if str(settings.get("trading_mode", "paper")).lower() == "live"
        else "paper"
    )
    today_count = _today_count(conn, user_id)
    raw_daily = _i(settings.get("max_trades_per_day", 5), 5)

    if len(rows) >= MAX_OPEN_POSITIONS:
        state["entry_permission"] = {
            "allowed": False,
            "reason": "MAX_OPEN_POSITIONS_REACHED",
            "open_positions": len(rows),
            "max_open_positions": MAX_OPEN_POSITIONS,
            "today_count": today_count,
            "mode": current_mode,
        }
        return False

    # A pending live-order lock must never starve paper trading after a mode
    # change or Railway restart. It remains enforced only in actual live mode.
    if current_mode == "live" and state.get("live_order_lock"):
        state["entry_permission"] = {
            "allowed": False,
            "reason": "LIVE_ORDER_PENDING_LOCK",
            "today_count": today_count,
            "mode": current_mode,
        }
        return False
    if current_mode == "paper":
        state.pop("live_order_lock", None)

    # Paper is the SaaS testing mode. Unless explicitly disabled, do not stop
    # qualified entries at the old five-trade default. Live keeps its limit.
    explicit_unlimited = _setting_truthy(
        settings.get("unlimited_trades"),
        default=_setting_truthy(
            settings.get("paper_unlimited_trades"),
            default=(current_mode == "paper"),
        ),
    )
    unlimited = raw_daily <= 0 or explicit_unlimited
    max_daily = max(1, raw_daily) if raw_daily > 0 else None

    if not unlimited and today_count >= max_daily:
        state["entry_permission"] = {
            "allowed": False,
            "reason": "DAILY_TRADE_LIMIT_REACHED",
            "today_count": today_count,
            "max_trades_per_day": max_daily,
            "mode": current_mode,
        }
        return False

    state["entry_permission"] = {
        "allowed": True,
        "reason": "ENTRY_PERMISSION_OK",
        "today_count": today_count,
        "max_trades_per_day": None if unlimited else max_daily,
        "unlimited": unlimited,
        "mode": current_mode,
    }
    return True
'''
if old_can_enter in text:
    text = text.replace(old_can_enter, new_can_enter, 1)
elif '"reason": "ENTRY_PERMISSION_OK"' not in text:
    raise SystemExit("_can_enter anchor not found")

failure_helper = '''def _record_preopen_failure(
    state,
    broker_name,
    selected,
    reason,
    stage,
    details=None,
):
    signal = dict((selected or {}).get("signal_data") or {})
    attempt = {
        "allowed": False,
        "reason": str(reason or "ENTRY_NOT_OPENED")[:240],
        "stage": str(stage or "PRE_OPEN"),
        "broker": str(broker_name or "unknown").lower(),
        "underlying": (selected or {}).get("underlying"),
        "side": signal.get("signal"),
        "score": signal.get("score"),
        "details": details or {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    state["entry_guard"] = dict(attempt)
    state["entry_attempt"] = dict(attempt)
    state["last_entry_attempt"] = dict(attempt)
    state["entry_block_reason"] = attempt["reason"]
    state["last_entry_block_reason"] = attempt["reason"]
    return False


'''
angel_anchor = 'def _open_angel(conn, user_id, obj, selected, settings, state):\n'
if 'def _record_preopen_failure(' not in text:
    if angel_anchor not in text:
        raise SystemExit("_open_angel anchor not found")
    text = text.replace(angel_anchor, failure_helper + angel_anchor, 1)

replacements = [
(
'''    if not resolved:
        return False

    resolved = dict(resolved)
''',
'''    if not resolved:
        return _record_preopen_failure(
            state,
            "angelone",
            selected,
            "OPTION_CONTRACT_NOT_RESOLVED",
            "OPTION_CONTRACT",
        )

    resolved = dict(resolved)
'''
),
(
'''    except Exception:
        return False

    quality = _legacy()._option_entry_quality_angel(
''',
'''    except Exception as exc:
        return _record_preopen_failure(
            state,
            "angelone",
            selected,
            "OPTION_LTP_FAILED",
            "OPTION_QUOTE",
            {"message": str(exc)[:180]},
        )

    quality = _legacy()._option_entry_quality_angel(
'''
),
(
'''    if not resolved.get("success"):
        return False

    if broker_name == "upstox":
''',
'''    if not resolved.get("success"):
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            resolved.get("message") or "OPTION_CONTRACT_NOT_RESOLVED",
            "OPTION_CONTRACT",
            {
                "reason": resolved.get("reason"),
                "errors": resolved.get("errors"),
                "requested_expiry": "current_week",
            },
        )

    if broker_name == "upstox":
'''
),
(
'''    if not quote.get("success"):
        return False

    quote_price = _f(quote.get("ltp"), 0)
    if quote_price <= 0:
        return False

    quality = _legacy()._option_entry_quality_multi(
''',
'''    if not quote.get("success"):
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            quote.get("message") or "OPTION_LTP_FAILED",
            "OPTION_QUOTE",
            {
                "symbol": resolved.get("symbol"),
                "token": resolved.get("token"),
                "exchange": resolved.get("exchange"),
            },
        )

    quote_price = _f(quote.get("ltp"), 0)
    if quote_price <= 0:
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "INVALID_OPTION_LTP",
            "OPTION_QUOTE",
            {"ltp": quote.get("ltp")},
        )

    quality = _legacy()._option_entry_quality_multi(
'''
),
(
'''    if slot is None or len(rows) >= MAX_OPEN_POSITIONS:
        return False
''',
'''    if slot is None or len(rows) >= MAX_OPEN_POSITIONS:
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "MAX_OPEN_POSITIONS_REACHED",
            "POSITION_LIMIT",
            {"open_positions": len(rows)},
        )
'''
),
(
'''        state["mode_change_blocked"] = (
            "Existing position close hone ke baad mode change apply hoga."
        )
        return False
''',
'''        state["mode_change_blocked"] = (
            "Existing position close hone ke baad mode change apply hoga."
        )
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "TRADING_MODE_CHANGE_BLOCKED",
            "MODE_GUARD",
            {"existing_modes": sorted(modes), "requested_mode": current_mode},
        )
'''
),
(
'''        if capital_base <= 0:
            state["live_order_error"] = "Broker available funds read nahi hue."
            return False
''',
'''        if capital_base <= 0:
            state["live_order_error"] = "Broker available funds read nahi hue."
            return _record_preopen_failure(
                state,
                broker_name,
                selected,
                "BROKER_FUNDS_UNAVAILABLE",
                "CAPITAL",
            )
'''
),
(
'''        state["position_size_block"] = {
            "slot": slot,
            "slot_budget": sizing["slot_budget"],
            "one_lot_cost": round(quote_price * sizing["lot_size"], 2),
            "reason": "Slot budget one lot se kam hai",
        }
        return False
''',
'''        state["position_size_block"] = {
            "slot": slot,
            "slot_budget": sizing["slot_budget"],
            "one_lot_cost": round(quote_price * sizing["lot_size"], 2),
            "reason": "Slot budget one lot se kam hai",
        }
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "POSITION_SIZE_BLOCK",
            "CAPITAL",
            dict(state["position_size_block"]),
        )
'''
),
(
'''    if current_mode == "live":
        if state.get("live_order_lock"):
            return False
''',
'''    if current_mode == "live":
        if state.get("live_order_lock"):
            return _record_preopen_failure(
                state,
                broker_name,
                selected,
                "LIVE_ORDER_PENDING_LOCK",
                "LIVE_ORDER",
            )
'''
),
(
'''            if order.get("pending"):
                state["live_order_lock"] = True
            return False

        entry = _f(order.get("avg_price"), quote_price)
''',
'''            if order.get("pending"):
                state["live_order_lock"] = True
            return _record_preopen_failure(
                state,
                broker_name,
                selected,
                order.get("message") or "LIVE_BUY_FAILED",
                "LIVE_ORDER",
                {"status": status, "order_id": order.get("order_id")},
            )

        entry = _f(order.get("avg_price"), quote_price)
'''
),
(
'''    if not trade_id:
        return False

    if current_mode == "live":
''',
'''    if not trade_id:
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "ATR_LEVELS_OR_TRADE_INSERT_FAILED",
            "TRADE_INSERT",
            {"spot_atr": market.get("atr"), "option_ltp": quote_price},
        )

    if current_mode == "live":
'''
),
]

for old, new in replacements:
    if old in text:
        text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")

test_path = Path("tests/test_qualified_paper_entry_permission_v4.py")
test_path.write_text(
'''import sqlite3

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
''',
    encoding="utf-8",
)
