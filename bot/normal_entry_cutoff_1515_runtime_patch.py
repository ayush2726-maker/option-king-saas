"""Final 15:15 IST cutoff for fresh normal AUTO entries.

Hero Zero has its own expiry route and window, so it is intentionally untouched.
Open normal positions keep their existing exit/trailing/EOD management.
"""

from __future__ import annotations

from datetime import datetime

from bot import auto_portfolio_runtime as runtime


VERSION = "OKAI-NORMAL-AUTO-CUTOFF-1515-V1"
ENTRY_START_MINUTE = 9 * 60 + 15
ENTRY_CUTOFF_MINUTE = 15 * 60 + 15
BLOCK_REASON = "NORMAL_AUTO_ENTRY_CUTOFF_1515_IST"


def _minute(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _entry_window_open(value: datetime) -> bool:
    return bool(
        value.weekday() < 5
        and ENTRY_START_MINUTE <= _minute(value) < ENTRY_CUTOFF_MINUTE
    )


def _reason(value: datetime) -> str:
    if value.weekday() >= 5:
        return "AUTO_ENTRY_BLOCKED_MARKET_CLOSED"
    if _minute(value) < ENTRY_START_MINUTE:
        return "AUTO_ENTRY_BLOCKED_BEFORE_0915_IST"
    return BLOCK_REASON


def _entry_window_state(value: datetime | None = None) -> dict:
    current = value or runtime._now_ist()
    reason = "" if _entry_window_open(current) else _reason(current)
    return {
        "open": not bool(reason),
        "reason": reason,
        "window_ist": "09:15-15:15",
        "checked_at_ist": current.isoformat(),
    }


def _mark(state: dict | None, value: datetime) -> None:
    if not isinstance(state, dict):
        return
    reason = _reason(value)
    state.update(
        {
            "entry_time_blocked": True,
            "entry_time_block_reason": reason,
            "entry_block_reason": reason,
            "last_entry_block_reason": reason,
            "entry_window_ist": "09:15-15:15",
            "hard_eod_exit_ist": "15:35",
            "selected_for_entry": None,
        }
    )


def _clear(state: dict | None) -> None:
    if not isinstance(state, dict):
        return
    state["entry_time_blocked"] = False
    state.pop("entry_time_block_reason", None)
    state["entry_window_ist"] = "09:15-15:15"
    state["hard_eod_exit_ist"] = "15:35"


def _sync_legacy_clocks() -> None:
    """Keep diagnostics and older guards on the same normal-entry clock."""
    try:
        from bot import eod_safety_testing_access_patch as eod

        eod.AUTO_ENTRY_CUTOFF_MINUTE = ENTRY_CUTOFF_MINUTE
        eod._entry_window_open = _entry_window_open
        eod._entry_block_reason = _reason
        eod._mark_entry_time_block = _mark
        eod._clear_entry_time_block = _clear
    except Exception:
        pass
    try:
        from bot import paper_market_close_1530_patch as paper

        paper.PAPER_ENTRY_CUTOFF_MINUTE = ENTRY_CUTOFF_MINUTE
        paper.LIVE_ENTRY_CUTOFF_MINUTE = ENTRY_CUTOFF_MINUTE
        paper._entry_window_open = _entry_window_open
        paper._entry_block_reason = _reason
        paper._window_labels = lambda _mode: ("09:15-15:15", "15:35")
    except Exception:
        pass
    try:
        from bot import real_mtf_session_guard_patch as mtf

        mtf.NORMAL_AUTO_CUTOFF_MINUTE = ENTRY_CUTOFF_MINUTE
        mtf._entry_window_state = _entry_window_state
    except Exception:
        pass
    try:
        from bot import trade_miss_audit_patch as audit

        audit.ENTRY_CUTOFF_HOUR = 15
        audit.ENTRY_CUTOFF_MINUTE = 15
    except Exception:
        pass


def apply_normal_entry_cutoff_1515_runtime_patch() -> bool:
    _sync_legacy_clocks()
    if getattr(runtime, "_okai_normal_entry_cutoff_1515_runtime_v1", False):
        return True

    previous_can_enter = runtime._can_enter
    previous_open = runtime._open_common

    def can_enter_with_1515_cutoff(conn, user_id, settings, rows, state):
        current = runtime._now_ist()
        if not _entry_window_open(current):
            _mark(state, current)
            return False
        _clear(state)
        return previous_can_enter(conn, user_id, settings, rows, state)

    def open_with_1515_cutoff(
        conn,
        user_id,
        broker_name,
        selected,
        settings,
        resolved,
        quote_price,
        quality,
        lot_size,
        live_order,
        live_cash,
        state,
    ):
        current = runtime._now_ist()
        if not _entry_window_open(current):
            _mark(state, current)
            return False
        _clear(state)
        return previous_open(
            conn,
            user_id,
            broker_name,
            selected,
            settings,
            resolved,
            quote_price,
            quality,
            lot_size,
            live_order,
            live_cash,
            state,
        )

    runtime._can_enter = can_enter_with_1515_cutoff
    runtime._open_common = open_with_1515_cutoff
    runtime._okai_normal_entry_cutoff_1515_runtime_v1 = True
    runtime._okai_normal_entry_cutoff_1515_runtime_version = VERSION
    return True


__all__ = [
    "BLOCK_REASON",
    "ENTRY_CUTOFF_MINUTE",
    "VERSION",
    "_entry_window_open",
    "apply_normal_entry_cutoff_1515_runtime_patch",
]
