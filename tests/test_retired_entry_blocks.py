from bot.entry_quality_v2_patch import _apply_entry_quality_v2
from bot.entry_timing_calibration_patch import _apply_timing_gate


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
