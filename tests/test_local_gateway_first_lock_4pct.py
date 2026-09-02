import sys
import types

# These tests exercise pure first-lock math only; avoid requiring the Angel SDK
# on the CI runner just to import the local gateway module.
smartapi_stub = types.ModuleType("SmartApi")


class _SmartConnectStub:
    pass


smartapi_stub.SmartConnect = _SmartConnectStub
sys.modules.setdefault("SmartApi", smartapi_stub)

from local_gateway_agent.okai_local_gateway_v2 import (
    FIRST_LOCK_NET_PERCENT,
    cost_safe_breakeven,
    dynamic_profit_lock,
)


def test_first_lock_threshold_is_charges_plus_four_percent():
    price, charges = cost_safe_breakeven(256.20, 40, "BFO")
    assert FIRST_LOCK_NET_PERCENT == 4.0
    assert charges > 0
    assert price > 256.20 * 1.04


def test_initial_atr_stop_is_untouched_before_four_percent_threshold():
    first_lock, _ = cost_safe_breakeven(256.20, 40, "BFO")
    result = dynamic_profit_lock(
        256.20,
        240.00,
        first_lock - 0.05,
        first_lock,
    )
    assert result["first_lock_triggered"] is False
    assert result["stage"] == "INITIAL_ATR_SL"
    assert result["sl_price"] == 240.00


def test_first_lock_arms_only_after_exact_threshold_is_seen():
    first_lock, _ = cost_safe_breakeven(256.20, 40, "BFO")
    result = dynamic_profit_lock(
        256.20,
        240.00,
        first_lock,
        first_lock,
    )
    assert result["first_lock_triggered"] is True
    assert result["stage"] in {
        "COST_SAFE_BE_PLUS_4PCT",
        "LOCK_0_5R_AFTER_4PCT",
        "DYNAMIC_TRAIL_0_8R_AFTER_4PCT",
    }
    assert result["sl_price"] >= first_lock


def test_old_point_eight_r_shortcut_cannot_arm_trail():
    # 0.9R is above the old 0.8R trigger, but still below charges+4% here.
    entry = 256.20
    initial_sl = 250.00
    first_lock, _ = cost_safe_breakeven(entry, 40, "BFO")
    peak = entry + 0.9 * (entry - initial_sl)
    assert peak < first_lock
    result = dynamic_profit_lock(entry, initial_sl, peak, first_lock)
    assert result["first_lock_triggered"] is False
    assert result["stage"] == "INITIAL_ATR_SL"
    assert result["sl_price"] == initial_sl
