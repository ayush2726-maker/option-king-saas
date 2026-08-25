import sqlite3

from bot import auto_portfolio_runtime as runtime
from bot.capital_based_sizing_restore_patch import _runtime_capital_size
from bot.expiry_hardlock_one_second_monitor_patch import (
    _attempt_qualified_candidates,
)
from bot.paper_unlimited_observation_patch import _can_enter_observation


def _row(**values):
    class Row(dict):
        def keys(self):
            return super().keys()

    return Row(values)


def _candidate(name, score):
    return {
        "underlying": name,
        "status": "OK",
        "signal_data": {
            "signal": "CE",
            "trade_allowed": True,
            "score": score,
            "base_score": score,
        },
        "market_data": {"adx": 30, "volume_ratio": 1.2},
    }


def _observation_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            status TEXT,
            pnl REAL,
            trading_mode TEXT,
            created_at TEXT
        )
        """
    )
    return conn


def test_final_capital_sizer_accepts_runtime_rows_keyword():
    sizing = _runtime_capital_size(100000, 1, 200, 30, rows=[])
    assert sizing["lots"] == 8
    assert sizing["qty"] == 240
    assert sizing["reserve_floor"] == 10000


def test_normal_runtime_quantity_caps_exact_planned_sl_loss_at_ten_percent():
    sizing = _runtime_capital_size(
        120000,
        1,
        100.0,
        20,
        rows=[],
        risk_points=26.0,
    )

    assert sizing["affordability_lots"] > sizing["lots"]
    assert sizing["risk_lots"] == 23
    assert sizing["lots"] == 23
    assert sizing["qty"] == 460
    assert sizing["max_planned_loss_amount"] == 12000.0
    assert sizing["planned_risk_per_lot"] == 520.0
    assert sizing["planned_risk_per_lot"] * sizing["lots"] <= 12000.0
    assert sizing["risk_sizing_mode"] == "NORMAL_PLANNED_SL_LOSS_CAP_10PCT"


def test_third_slot_can_use_only_remainder_above_reserve():
    existing = [_row(capital_used=70000)]
    sizing = _runtime_capital_size(100000, 3, 200, 30, rows=existing)
    assert sizing["lots"] == 1
    assert sizing["capital_used"] == 6000
    assert sizing["flex_used"] is True

    blocked = _runtime_capital_size(
        100000,
        3,
        200,
        30,
        rows=[_row(capital_used=85000)],
    )
    assert blocked["lots"] == 0


def test_one_second_runtime_falls_back_to_next_qualified_candidate():
    scans = [_candidate("NIFTY", 90), _candidate("SENSEX", 85)]
    state = {}
    seen = []

    def opener(candidate):
        seen.append(candidate["underlying"])
        return candidate["underlying"] == "SENSEX"

    selected = _attempt_qualified_candidates(scans, set(), opener, state)
    assert seen == ["NIFTY", "SENSEX"]
    assert selected["underlying"] == "SENSEX"


def test_paper_observation_clears_stale_live_order_and_mode_locks():
    conn = _observation_conn()
    state = {
        "live_order_lock": True,
        "live_order_lock_source": "ENTRY_BUY_PENDING",
        "mode_change_blocked": "old state",
    }
    try:
        allowed = _can_enter_observation(
            conn,
            1,
            {"trading_mode": "paper"},
            [],
            state,
        )
    finally:
        conn.close()

    assert allowed is True
    assert "live_order_lock" not in state
    assert "live_order_lock_source" not in state
    assert "mode_change_blocked" not in state


def test_mode_change_flag_remains_only_for_real_conflicting_position():
    state = {"mode_change_blocked": "old state"}
    assert runtime._reconcile_mode_change_state([], "paper", state) is False
    assert "mode_change_blocked" not in state

    live_row = _row(trading_mode="live")
    assert runtime._reconcile_mode_change_state([live_row], "paper", state) is True
