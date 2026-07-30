from datetime import date

import bot.broker_expiry_guard as expiry_guard
from bot.broker_expiry_guard import _max_normal_dte, _validate_result
from bot.option_chain import expected_expiry_for_trade_date
from bot.brokers.upstox_nearest_expiry_patch import (
    _pick_contract as pick_upstox_contract,
    _validate_result as validate_upstox_result,
)


def _contract(
    underlying,
    expiry,
    strike,
    side,
    *,
    weekly=False,
    symbol=None,
):
    return {
        "underlying_symbol": underlying,
        "instrument_type": side,
        "expiry": expiry,
        "strike_price": strike,
        "trading_symbol": symbol or f"{underlying}-{expiry}-{strike}-{side}",
        "instrument_key": f"KEY-{underlying}-{expiry}-{strike}-{side}",
        "segment": "BSE_FO" if underlying == "SENSEX" else "NSE_FO",
        "lot_size": 1,
        "weekly": weekly,
    }


def test_current_index_expiry_calendars():
    trade_day = date(2026, 7, 30)
    assert expected_expiry_for_trade_date("NIFTY", trade_day) == date(2026, 8, 4)
    assert expected_expiry_for_trade_date("BANKNIFTY", trade_day) == date(2026, 8, 25)
    assert expected_expiry_for_trade_date("SENSEX", trade_day) == date(2026, 7, 30)


def test_valid_nearest_contracts_are_not_blocked():
    today = date(2026, 7, 30)
    cases = {
        "NIFTY": "2026-08-04",
        "SENSEX": "2026-07-30",
        "BANKNIFTY": "2026-08-25",
    }
    for underlying, expiry in cases.items():
        result = _validate_result(
            {
                "success": True,
                "expiry": expiry,
                "symbol": f"{underlying}-VALID",
                "token": f"{underlying}-TOKEN",
            },
            underlying,
            "current_week",
            today=today,
        )
        assert result["success"] is True
        assert result["symbol"] == f"{underlying}-VALID"
        assert result["token"] == f"{underlying}-TOKEN"


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


def test_upstox_nifty_picks_weekly_even_when_monthly_row_is_first():
    today = date(2026, 7, 30)
    rows = [
        _contract("NIFTY", "2026-08-25", 24250, "PE", weekly=False),
        _contract("NIFTY", "2026-08-04", 24250, "PE", weekly=True),
    ]
    expiry_day, row = pick_upstox_contract(
        rows,
        "NIFTY",
        24250,
        "PE",
        today,
    )
    assert expiry_day == date(2026, 8, 4)
    assert row["weekly"] is True


def test_upstox_banknifty_picks_nearest_monthly_not_later_month():
    today = date(2026, 7, 30)
    rows = [
        _contract("BANKNIFTY", "2026-09-29", 52000, "CE"),
        _contract("BANKNIFTY", "2026-08-25", 52000, "CE"),
    ]
    expiry_day, row = pick_upstox_contract(
        rows,
        "BANKNIFTY",
        52000,
        "CE",
        today,
    )
    assert expiry_day == date(2026, 8, 25)
    assert row["expiry"] == "2026-08-25"


def test_upstox_sensex_same_day_contract_is_allowed():
    today = date(2026, 7, 30)
    result = validate_upstox_result(
        {
            "success": True,
            "expiry": "2026-07-30",
            "symbol": "SENSEX-SAME-DAY",
            "token": "SENSEX-TOKEN",
        },
        "SENSEX",
        "current_week",
        today,
        "TEST",
    )
    assert result["success"] is True
    assert result["expiry_dte"] == 0
    assert result["symbol"] == "SENSEX-SAME-DAY"


def test_broker_install_wrapper_keeps_valid_upstox_trade_openable():
    class FakeAngel:
        pass

    class FakeZerodha:
        pass

    class FakeUpstox:
        def search_option(self, underlying, expiry, strike, option_type):
            return {
                "success": True,
                "expiry": "2026-08-04",
                "symbol": "NIFTY-VALID",
                "token": "NIFTY-TOKEN",
                "strike": strike,
            }

    original_today = expiry_guard._today_ist
    expiry_guard._today_ist = lambda: date(2026, 7, 30)
    try:
        expiry_guard.install(FakeAngel, FakeZerodha, FakeUpstox)
        result = FakeUpstox().search_option(
            "NIFTY",
            "current_week",
            24250,
            "PE",
        )
    finally:
        expiry_guard._today_ist = original_today

    assert result["success"] is True
    assert result["symbol"] == "NIFTY-VALID"
