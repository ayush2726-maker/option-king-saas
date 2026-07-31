from datetime import datetime, timedelta, timezone

from bot import paper_market_close_1530_patch as patch


IST = timezone(timedelta(hours=5, minutes=30))


def _friday(hour, minute):
    return datetime(2026, 7, 31, hour, minute, tzinfo=IST)


def test_paper_entries_stop_at_1525():
    with patch._entry_mode("paper"):
        assert patch._entry_window_open(_friday(14, 46)) is True
        assert patch._entry_window_open(_friday(15, 24)) is True
        assert patch._entry_window_open(_friday(15, 25)) is False
        assert patch._entry_block_reason(_friday(15, 25)) == (
            "PAPER_ENTRY_CUTOFF_1525_IST"
        )


def test_live_safety_window_stays_1445():
    with patch._entry_mode("live"):
        assert patch._entry_window_open(_friday(14, 44)) is True
        assert patch._entry_window_open(_friday(14, 45)) is False
        assert patch._entry_block_reason(_friday(14, 45)) == (
            "LIVE_ENTRY_CUTOFF_1445_IST"
        )


def test_paper_eod_is_1525():
    assert patch._adjust_eod_reason(
        "paper",
        None,
        _friday(15, 24),
    ) is None
    assert patch._adjust_eod_reason(
        "paper",
        "EOD EXIT 15:25 IST",
        _friday(15, 25),
    ) == "PAPER EOD EXIT 15:25 IST"
    assert patch._adjust_eod_reason(
        "paper",
        "PAPER EOD EXIT 15:30 IST",
        _friday(15, 25),
    ) == "PAPER EOD EXIT 15:25 IST"


def test_sl_structural_and_live_exit_reasons_are_preserved():
    structural = "TWO CANDLE STRUCTURAL REVERSAL EXIT | count=2"
    assert patch._adjust_eod_reason(
        "paper",
        structural,
        _friday(15, 25),
    ) == structural
    assert patch._adjust_eod_reason(
        "live",
        "EOD EXIT 15:25 IST",
        _friday(15, 25),
    ) == "EOD EXIT 15:25 IST"


def test_state_labels_match_mode():
    state = {}
    with patch._entry_mode("paper"):
        patch._clear_entry_time_block(state)
    assert state["entry_window_ist"] == "09:15-15:25"
    assert state["hard_eod_exit_ist"] == "15:25"
    assert state["paper_testing_until_market_close"] is True
    assert state["paper_safe_close_buffer_minutes"] == 5

    with patch._entry_mode("live"):
        patch._clear_entry_time_block(state)
    assert state["entry_window_ist"] == "09:15-14:45"
    assert state["hard_eod_exit_ist"] == "15:25"
    assert state["paper_testing_until_market_close"] is False
    assert state["paper_safe_close_buffer_minutes"] == 0
