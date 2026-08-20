"""Keep PAPER testing active until the safer 15:25 IST cutoff.

PAPER and LIVE modes may accept fresh AUTO entries until 15:25. Open positions
are force-closed at 15:35, five minutes before the extended 15:40 market close.
Backtests are unchanged.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from bot import auto_portfolio_runtime as runtime
from bot import eod_safety_testing_access_patch as eod_patch


ENTRY_START_MINUTE = 9 * 60 + 15
PAPER_ENTRY_CUTOFF_MINUTE = 15 * 60 + 25
PAPER_EOD_MINUTE = 15 * 60 + 35
LIVE_ENTRY_CUTOFF_MINUTE = 15 * 60 + 25
LIVE_EOD_MINUTE = 15 * 60 + 35
PATCH_VERSION = "PAPER_SAFE_CLOSE_1535_V3"

_context = threading.local()


def _now_ist() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _minute_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _mode_from_settings(settings) -> str:
    value = "paper"
    if isinstance(settings, dict):
        value = str(settings.get("trading_mode", "paper") or "paper").lower()
    return "live" if value == "live" else "paper"


def _current_entry_mode() -> str:
    value = str(getattr(_context, "entry_mode", "live") or "live").lower()
    return "paper" if value == "paper" else "live"


@contextmanager
def _entry_mode(mode: str):
    previous = getattr(_context, "entry_mode", None)
    _context.entry_mode = "paper" if str(mode).lower() == "paper" else "live"
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_context, "entry_mode")
            except Exception:
                pass
        else:
            _context.entry_mode = previous


def _entry_cutoff(mode: str) -> int:
    return (
        PAPER_ENTRY_CUTOFF_MINUTE
        if str(mode).lower() == "paper"
        else LIVE_ENTRY_CUTOFF_MINUTE
    )


def _entry_window_open(value: datetime | None = None) -> bool:
    current = value or _now_ist()
    minute = _minute_of_day(current)
    return bool(
        current.weekday() < 5
        and ENTRY_START_MINUTE <= minute < _entry_cutoff(_current_entry_mode())
    )


def _entry_block_reason(value: datetime) -> str:
    if value.weekday() >= 5:
        return "AUTO_ENTRY_BLOCKED_MARKET_CLOSED"
    if _minute_of_day(value) < ENTRY_START_MINUTE:
        return "AUTO_ENTRY_BLOCKED_BEFORE_0915_IST"
    if _current_entry_mode() == "paper":
        return "PAPER_ENTRY_CUTOFF_1525_IST"
    return "LIVE_ENTRY_CUTOFF_1525_IST"


def _window_labels(mode: str) -> tuple[str, str]:
    if str(mode).lower() == "paper":
        return "09:15-15:25", "15:35"
    return "09:15-15:25", "15:35"


def _mark_entry_time_block(state: dict | None, value: datetime) -> None:
    if not isinstance(state, dict):
        return
    mode = _current_entry_mode()
    window, hard_eod = _window_labels(mode)
    state.update(
        {
            "entry_time_blocked": True,
            "entry_time_block_reason": _entry_block_reason(value),
            "entry_window_ist": window,
            "hard_eod_exit_ist": hard_eod,
            "paper_testing_until_market_close": mode == "paper",
            "paper_safe_close_buffer_minutes": 5 if mode == "paper" else 0,
            "selected_for_entry": None,
        }
    )


def _clear_entry_time_block(state: dict | None) -> None:
    if not isinstance(state, dict):
        return
    mode = _current_entry_mode()
    window, hard_eod = _window_labels(mode)
    state["entry_time_blocked"] = False
    state.pop("entry_time_block_reason", None)
    state["entry_window_ist"] = window
    state["hard_eod_exit_ist"] = hard_eod
    state["paper_testing_until_market_close"] = mode == "paper"
    state["paper_safe_close_buffer_minutes"] = 5 if mode == "paper" else 0


def _adjust_eod_reason(mode: str, reason, value: datetime):
    """Force PAPER EOD at 15:35 while preserving SL and structural exits."""
    if str(mode).lower() != "paper":
        return reason

    minute = _minute_of_day(value)
    text = str(reason or "").upper().strip()
    old_eod = text in {
        "EOD EXIT 15:25 IST",
        "PAPER EOD EXIT 15:30 IST",
    }

    if minute < PAPER_EOD_MINUTE and old_eod:
        return None
    if minute >= PAPER_EOD_MINUTE and (reason is None or old_eod):
        return "PAPER EOD EXIT 15:35 IST"
    return reason


def apply_paper_market_close_1530_patch() -> None:
    """Compatibility entry point; installs the corrected 15:25 PAPER window."""
    if getattr(runtime, "_okai_paper_safe_close_1525_v2", False):
        return

    # The existing EOD wrappers resolve these helpers dynamically, so a
    # thread-local mode context lets PAPER and LIVE keep different entry windows.
    eod_patch._entry_window_open = _entry_window_open
    eod_patch._entry_block_reason = _entry_block_reason
    eod_patch._mark_entry_time_block = _mark_entry_time_block
    eod_patch._clear_entry_time_block = _clear_entry_time_block
    eod_patch.HARD_EOD_MINUTE = PAPER_EOD_MINUTE

    previous_can_enter = runtime._can_enter
    previous_open_common = runtime._open_common
    previous_evaluate_exit = runtime._evaluate_exit

    def can_enter_with_mode_context(conn, user_id, settings, rows, state):
        mode = _mode_from_settings(settings)
        with _entry_mode(mode):
            return previous_can_enter(conn, user_id, settings, rows, state)

    def open_common_with_mode_context(
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
        mode = _mode_from_settings(settings)
        with _entry_mode(mode):
            return previous_open_common(
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

    def evaluate_with_paper_safe_close(trade, ltp, market_data, candle_id):
        result = dict(
            previous_evaluate_exit(trade, ltp, market_data, candle_id) or {}
        )
        mode = runtime._mode(trade)
        result["reason"] = _adjust_eod_reason(
            mode,
            result.get("reason"),
            _now_ist(),
        )
        result["paper_market_close_patch"] = PATCH_VERSION
        return result

    runtime._can_enter = can_enter_with_mode_context
    runtime._open_common = open_common_with_mode_context
    runtime._evaluate_exit = evaluate_with_paper_safe_close
    runtime._okai_paper_safe_close_1525_v2 = True
    runtime._okai_paper_market_close_1530_v1 = True

    # Trade-miss audit is display-only. During PAPER testing, use the actual
    # 15:25 entry cutoff instead of the old 14:45 warning.
    try:
        from bot import trade_miss_audit_patch as audit

        audit.ENTRY_CUTOFF_HOUR = 15
        audit.ENTRY_CUTOFF_MINUTE = 25
    except Exception:
        pass
