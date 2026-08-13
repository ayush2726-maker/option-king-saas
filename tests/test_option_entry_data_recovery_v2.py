from datetime import date

import bot.expiry_entry_diagnostics_patch as expiry_patch
from bot.auto_entry_attempt_diagnostics_patch import _fetch_multi_ltp


class FakeUpstox:
    BASE_URL = "https://api.upstox.test/v2"

    def _h(self):
        return {"Authorization": "Bearer test"}


def _contract(expiry, strike=24400, side="CE"):
    return {
        "underlying_symbol": "NIFTY",
        "instrument_type": side,
        "expiry": expiry,
        "strike_price": strike,
        "trading_symbol": f"NIFTY-{expiry}-{strike}-{side}",
        "instrument_key": f"NSE_FO|NIFTY-{expiry}-{strike}-{side}",
        "segment": "NSE_FO",
        "lot_size": 65,
    }


def test_actual_entry_uses_concrete_18_aug_expiry(monkeypatch):
    calls = []

    def fake_contracts(_obj, underlying, expiry_value):
        calls.append((underlying, expiry_value))
        return [_contract("2026-08-18")], None

    monkeypatch.setattr(expiry_patch, "_today_ist", lambda: date(2026, 8, 13))
    monkeypatch.setattr(expiry_patch, "_upstox_option_contracts", fake_contracts)

    result = expiry_patch._search_upstox_nearest(
        FakeUpstox(),
        "NIFTY",
        "current_week",
        24400,
        "CE",
    )

    assert result["success"] is True
    assert result["expiry"] == "2026-08-18"
    assert result["expected_expiry"] == "2026-08-18"
    assert result["token"].startswith("NSE_FO|")
    assert calls == [("NIFTY", "2026-08-18")]


def test_contract_resolver_retries_one_transient_empty_response(monkeypatch):
    calls = []

    def fake_contracts(_obj, _underlying, expiry_value):
        calls.append(expiry_value)
        if len(calls) == 1:
            return [], "temporary timeout"
        return [_contract("2026-08-18")], None

    monkeypatch.setattr(expiry_patch, "_today_ist", lambda: date(2026, 8, 13))
    monkeypatch.setattr(expiry_patch, "_upstox_option_contracts", fake_contracts)
    monkeypatch.setattr(expiry_patch.time, "sleep", lambda _seconds: None)

    result = expiry_patch._search_upstox_nearest(
        FakeUpstox(),
        "NIFTY",
        "current_week",
        24400,
        "CE",
    )

    assert result["success"] is True
    assert result["request_count"] == 2
    assert calls == ["2026-08-18", "2026-08-18"]


def test_unfiltered_recovery_never_selects_farther_week(monkeypatch):
    calls = []

    def fake_contracts(_obj, _underlying, expiry_value):
        calls.append(expiry_value)
        if expiry_value is not None:
            return [], "exact filter temporarily empty"
        return [
            _contract("2026-08-25"),
            _contract("2026-08-18"),
        ], None

    monkeypatch.setattr(expiry_patch, "_today_ist", lambda: date(2026, 8, 13))
    monkeypatch.setattr(expiry_patch, "_upstox_option_contracts", fake_contracts)
    monkeypatch.setattr(expiry_patch.time, "sleep", lambda _seconds: None)

    result = expiry_patch._search_upstox_nearest(
        FakeUpstox(),
        "NIFTY",
        "current_week",
        24400,
        "CE",
    )

    assert result["success"] is True
    assert result["expiry"] == "2026-08-18"
    assert result["expiry_source"] == "UNFILTERED_STRICT_RECOVERY"
    assert calls == ["2026-08-18", "2026-08-18", None]


def test_upstox_ltp_retries_and_uses_instrument_key():
    class QuoteClient:
        def __init__(self):
            self.calls = []

        def get_ltp(self, identifier, exchange):
            self.calls.append((identifier, exchange))
            if len(self.calls) < 3:
                return {"success": False, "message": "temporary quote miss"}
            return {"success": True, "ltp": 143.35}

    client = QuoteClient()
    quote, errors = _fetch_multi_ltp(
        client,
        "upstox",
        {
            "symbol": "NIFTY 24400 CE",
            "token": "NSE_FO|NIFTY24400CE",
            "exchange": "NSE_FO",
        },
        sleeper=lambda _seconds: None,
    )

    assert quote == {"success": True, "ltp": 143.35}
    assert errors == ["temporary quote miss", "temporary quote miss"]
    assert client.calls == [
        ("NSE_FO|NIFTY24400CE", "NSE_FO"),
        ("NSE_FO|NIFTY24400CE", "NSE_FO"),
        ("NSE_FO|NIFTY24400CE", "NSE_FO"),
    ]


def test_generic_combined_data_reason_is_not_emitted():
    reason = expiry_patch._execution_reason(
        {
            "last_entry_attempt": {
                "allowed": False,
                "reason": "OPTION_LTP_FAILED:temporary quote miss",
            }
        }
    )

    assert reason == "OPTION_LTP_FAILED:temporary quote miss"
    assert reason != "OPTION_RESOLVE_LTP_OR_ATR_FAILED"
