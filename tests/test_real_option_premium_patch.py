from copy import deepcopy

from backtest import routes
from backtest.real_option_premium_patch import (
    REAL_PREMIUM_MODEL,
    _build_option_bars,
    _extract_expiries,
    _normalise_contract_rows,
    _real_premium_bar,
    _real_premium_prepare_entry,
)


def test_extract_expiries_accepts_string_and_object_payloads():
    payload = {
        "data": [
            "2026-07-28",
            {"expiry_date": "2026-08-04"},
            {"expiry": "2026-07-28"},
            "bad-value",
        ]
    }
    assert _extract_expiries(payload) == ["2026-07-28", "2026-08-04"]


def test_normalise_contract_rows_keeps_real_option_identity():
    payload = {
        "data": [
            {
                "instrument_key": "NSE_FO|12345",
                "trading_symbol": "NIFTY28JUL2624000PE",
                "instrument_type": "PE",
                "expiry": "2026-07-28",
                "strike_price": 24000,
                "lot_size": 65,
            }
        ]
    }
    rows = _normalise_contract_rows(payload)
    assert rows == [
        {
            "instrument_key": "NSE_FO|12345",
            "symbol": "NIFTY28JUL2624000PE",
            "expiry": "2026-07-28",
            "strike": 24000.0,
            "option_type": "PE",
            "lot_size": 65,
            "segment": "",
        }
    ]


def test_real_entry_uses_next_option_candle_open_without_lookahead():
    class FakeBroker:
        def resolve_historical_option(self, instrument, date_str, spot, side):
            return {
                "success": True,
                "symbol": "NIFTY28JUL2624000PE",
                "instrument_key": "NSE_FO|12345",
                "expiry": "2026-07-28",
                "strike": 24000.0,
                "option_type": "PE",
                "lot_size": 65,
                "expired": True,
            }

        def get_historical_option_candles(self, contract, start, end, interval):
            return {
                "success": True,
                "request_mode": "UPSTOX_EXPIRED_INSTRUMENTS",
                "candles": [
                    ["2026-07-22T10:00:00+05:30", 100, 102, 98, 101, 10, 20],
                    ["2026-07-22T10:01:00+05:30", 103, 108, 101, 106, 12, 22],
                    ["2026-07-22T10:02:00+05:30", 107, 111, 105, 109, 14, 24],
                ],
            }

    old_lot = routes.LOT_SIZES.get("NIFTY")
    try:
        result = _real_premium_prepare_entry(
            broker_name="upstox",
            obj=FakeBroker(),
            instrument="NIFTY",
            date_str="2026-07-22",
            entry_time="2026-07-22T10:00:00+05:30",
            spot_price=24012,
            side="PE",
        )
        assert result["success"] is True
        assert result["entry_price"] == 103.0
        assert result["entry_time"] == "2026-07-22T10:01:00+05:30"
        assert result["execution_model"] == "NEXT_OPTION_CANDLE_OPEN"
        assert result["premium_source"] == REAL_PREMIUM_MODEL
        assert routes.LOT_SIZES["NIFTY"] == 65

        trade = {
            "_real_option_bars": result["bars"],
            "_real_option_bar_keys": result["bar_keys"],
            "_real_option_entry_minute": result["entry_minute"],
        }
        bar = _real_premium_bar(trade, "2026-07-22T10:02:00+05:30")
        assert bar["high"] == 111.0
        assert bar["low"] == 105.0
        assert bar["close"] == 109.0
    finally:
        if old_lot is None:
            routes.LOT_SIZES.pop("NIFTY", None)
        else:
            routes.LOT_SIZES["NIFTY"] = old_lot


def test_real_entry_rejects_non_upstox_broker():
    result = _real_premium_prepare_entry(
        broker_name="angelone",
        obj=object(),
        instrument="NIFTY",
        date_str="2026-07-22",
        entry_time="2026-07-22T10:00:00+05:30",
        spot_price=24000,
        side="CE",
    )
    assert result["success"] is False
    assert result["message"] == "REAL_PREMIUM_REQUIRES_UPSTOX_BROKER"
