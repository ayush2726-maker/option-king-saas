from datetime import datetime, timedelta, timezone

from bot import paper_market_close_1530_patch as patch


IST = timezone(timedelta(hours=5, minutes=30))


def _friday(hour, minute):
    return datetime(2026, 7, 31, hour, minute, tzinfo=IST)


def test_paper_entries_remain_open_until_1530():
    with patch._entry_mode("paper"):
        assert patch._entry_window_open(_friday(14, 46)) is True
        assert patch._entry_window_open(_friday(15, 29)) is True
        assert patch._entry_window_open(_friday(15, 30)) is False
        assert patch._entry_block_reason(_friday(15, 30)) == (
            "PAPER_ENTRY_CUTOFF_1530_IST"
        )


def test_live_safety_window_stays_1445():
    with patch._entry_mode("live"):
        assert patch._entry_window_open(_friday(14, 44)) is True
        assert patch._entry_window_open(_friday(14, 45)) is False
        assert patch._entry_block_reason(_friday(14, 45)) == (
            "LIVE_ENTRY_CUTOFF_1445_IST"
        )


def test_old_paper_1525_eod_is_delayed_to_market_close():
    assert patch._adjust_eod_reason(
        "paper",
        "EOD EXIT 15:25 IST",
        _friday(15, 25),
    ) is None
    assert patch._adjust_eod_reason(
        "paper",
        "EOD EXIT 15:25 IST",
        _friday(15, 29),
    ) is None
    assert patch._adjust_eod_reason(
        "paper",
        "EOD EXIT 15:25 IST",
        _friday(15, 30),
    ) == "PAPER EOD EXIT 15:30 IST"


def test_sl_structural_and_live_exit_reasons_are_preserved():
    structural = "TWO CANDLE STRUCTURAL REVERSAL EXIT | count=2"
    assert patch._adjust_eod_reason(
        "paper",
        structural,
        _friday(15, 30),
    ) == structural
    assert patch._adjust_eod_reason(
        "live",
        "EOD EXIT 15:25 IST",
        _friday(15, 30),
    ) == "EOD EXIT 15:25 IST"


def test_state_labels_match_mode():
    state = {}
    with patch._entry_mode("paper"):
        patch._clear_entry_time_block(state)
    assert state["entry_window_ist"] == "09:15-15:30"
    assert state["hard_eod_exit_ist"] == "15:30"
    assert state["paper_testing_until_market_close"] is True

    with patch._entry_mode("live"):
        patch._clear_entry_time_block(state)
    assert state["entry_window_ist"] == "09:15-14:45"
    assert state["hard_eod_exit_ist"] == "15:25"
    assert state["paper_testing_until_market_close"] is False
