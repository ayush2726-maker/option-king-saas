from bot.entry_quality_v2_patch import _apply_entry_quality_v2
from bot.entry_timing_calibration_patch import _apply_timing_gate
from bot.qualified_entry_release_patch import _repair_signal as _repair_qualified_signal


RETIRED_REASONS = {
    "EMA_EXTENSION_OVER_0.95_ATR",
    "ORB_OR_MOMENTUM_TRIGGER_REQUIRED",
}


def test_retired_reasons_cannot_survive_or_keep_qualified_setup_blocked():
    market = {
        "price": 100.0,
        "ema9": 110.0,
        "vwap": 105.0,
        "atr": 10.0,
        "vwap_fallback_used": False,
        "orb_high": 120.0,
        "orb_low": 80.0,
        "c1_bullish": True,
        "c2_bullish": False,
    }
    signal = {
        "signal": "WAIT",
        "candidate_signal": "PE",
        "score": 89,
        "min_score": 82,
        "trade_allowed": False,
        "mandatory_confirmations_passed": True,
        "safety_gate_reasons": list(RETIRED_REASONS),
        "fresh_entry_block_reasons": list(RETIRED_REASONS),
        "warnings": [
            "ENTRY_TIMING_BLOCK:EMA_EXTENSION_OVER_0.95_ATR",
            "FRESH_TRIGGER_MISSING:ORB_OR_MOMENTUM",
        ],
    }

    quality = _apply_entry_quality_v2(signal, market, None)
    output = _apply_timing_gate(quality, market)

    assert output["signal"] == "PE"
    assert output["trade_allowed"] is True
    assert output["fresh_trigger_passed"] is False
    assert output["fresh_trigger_is_blocking"] is False
    assert RETIRED_REASONS.isdisjoint(output["safety_gate_reasons"])
    assert RETIRED_REASONS.isdisjoint(output["fresh_entry_block_reasons"])
    assert all(
        not any(reason in str(warning) for reason in RETIRED_REASONS)
        for warning in output["warnings"]
    )


def test_unrelated_shared_anti_chase_still_blocks_after_cleanup():
    market = {
        "price": 100.0,
        "ema9": 110.0,
        "vwap": 105.0,
        "atr": 10.0,
        "vwap_fallback_used": False,
        "orb_high": 120.0,
        "orb_low": 80.0,
        "c1_bullish": True,
        "c2_bullish": False,
    }
    signal = {
        "signal": "WAIT",
        "candidate_signal": "PE",
        "score": 89,
        "min_score": 82,
        "trade_allowed": False,
        "mandatory_confirmations_passed": True,
        "safety_gate_reasons": [
            "EMA_EXTENSION_OVER_0.95_ATR",
            "EMA_ANTI_CHASE",
        ],
        "fresh_entry_block_reasons": [],
        "warnings": [],
    }

    quality = _apply_entry_quality_v2(signal, market, None)
    output = _apply_timing_gate(quality, market)

    assert output["signal"] == "WAIT"
    assert output["trade_allowed"] is False
    assert output["safety_gate_reasons"] == ["EMA_ANTI_CHASE"]

def test_orb_extension_is_observation_only_for_qualified_entry():
    signal = {
        "signal": "WAIT",
        "candidate_signal": "PE",
        "score": 89,
        "min_score": 82,
        "trade_allowed": False,
        "safety_gate_reasons": ["ORB_EXTENSION_OVER_1.35_ATR"],
        "fresh_entry_block_reasons": ["ORB_EXTENSION_OVER_1.35_ATR"],
        "warnings": ["FRESH_ENTRY_BLOCK:ORB_EXTENSION_OVER_1.35_ATR"],
        "orb_extension_atr": 1.82,
    }

    output = _repair_qualified_signal(signal)

    assert output["signal"] == "PE"
    assert output["trade_allowed"] is True
    assert output["safety_gate_passed"] is True
    assert output["fresh_entry_ok"] is True
    assert output["orb_extension_blocking"] is False
    assert output["orb_extension_atr"] == 1.82
    assert "ORB_EXTENSION_OVER_1.35_ATR" not in output["safety_gate_reasons"]
    assert "ORB_EXTENSION_OVER_1.35_ATR" not in output["fresh_entry_block_reasons"]
    assert all(
        "ORB_EXTENSION_OVER_1.35_ATR" not in str(warning)
        for warning in output["warnings"]
    )

