from datetime import date

from bot.broker_expiry_guard import _max_normal_dte, _validate_result
from bot.option_chain import expected_expiry_for_trade_date


def test_current_index_expiry_calendars():
    trade_day = date(2026, 7, 30)
    assert expected_expiry_for_trade_date("NIFTY", trade_day) == date(2026, 8, 4)
    assert expected_expiry_for_trade_date("BANKNIFTY", trade_day) == date(2026, 8, 25)
    assert expected_expiry_for_trade_date("SENSEX", trade_day) == date(2026, 7, 30)


def test_weekly_indices_reject_far_monthly_contract():
    today = date(2026, 7, 30)
    for underlying in ("NIFTY", "SENSEX"):
        result = _validate_result(
            {"success": True, "expiry": "2026-08-25"},
            underlying,
            "current_week",
            today=today,
        )
        assert result["success"] is False
        assert result["message"] == "EXPIRY_TOO_FAR"
        assert result["max_expiry_dte"] == 8


def test_banknifty_nearest_monthly_contract_is_allowed():
    today = date(2026, 7, 30)
    result = _validate_result(
        {"success": True, "expiry": "2026-08-25"},
        "BANKNIFTY",
        "current_week",
        today=today,
    )
    assert result["success"] is True
    assert result["expiry_dte"] == 26
    assert _max_normal_dte("BANKNIFTY") == 40


def test_explicit_expiry_must_match_exactly():
    result = _validate_result(
        {"success": True, "expiry": "2026-08-04"},
        "NIFTY",
        "2026-08-11",
        today=date(2026, 7, 30),
    )
    assert result["success"] is False
    assert result["message"] == "EXPIRY_MISMATCH"
