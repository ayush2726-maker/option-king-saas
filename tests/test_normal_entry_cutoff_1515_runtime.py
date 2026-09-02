from datetime import datetime
from zoneinfo import ZoneInfo

from bot.normal_entry_cutoff_1515_runtime_patch import (
    BLOCK_REASON,
    ENTRY_CUTOFF_MINUTE,
    _entry_window_state,
    _entry_window_open,
)


IST = ZoneInfo("Asia/Kolkata")


def test_normal_auto_cutoff_is_exactly_1515_ist():
    assert ENTRY_CUTOFF_MINUTE == 15 * 60 + 15
    assert _entry_window_open(datetime(2026, 9, 2, 15, 14, tzinfo=IST)) is True
    assert _entry_window_open(datetime(2026, 9, 2, 15, 15, tzinfo=IST)) is False
    state = _entry_window_state(datetime(2026, 9, 2, 15, 15, tzinfo=IST))
    assert state["reason"] == BLOCK_REASON
    assert state["window_ist"] == "09:15-15:15"
