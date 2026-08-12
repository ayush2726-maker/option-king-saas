from bot.live_score_breakdown_patch import _score_payload
from bot.entry_quality_v2_patch import _apply_entry_quality_v2
from bot.fresh_entry_guard_patch import _apply_fresh_entry_guard
from bot.strategy import calculate_tqu_score, get_full_signal


def _four_of_five_pe_market():
    return {
        "price": 100.0,
        "vwap": 101.0,
        "ema9": 100.0,
        "ema21": 101.0,
        "adx": 32.4,
        "volume_ratio": 0.0,
        "volume_available": False,
        "supertrend_dir": "DOWN",
        "trend": "DOWNTREND",
        "mtf_confirmed": True,
        "orb_high": 120.0,
        "orb_low": 106.0,
        "c1_bullish": False,
        "c2_bullish": True,
        "atr": 10.0,
    }


def test_unavailable_index_volume_is_normalized_without_lowering_threshold():
    result = calculate_tqu_score(
        base_score=44,
        adx=32.4,
        volume_ratio=0.0,
        mtf_confirmed=True,
        is_sideways=False,
        volume_available=False,
    )

    assert result["pre_normalization_score"] == 76
    assert result["effective_score_max"] == 92
    assert result["score"] == 83
    assert result["availability_adjustment"] == 7
    assert result["availability_normalized"] is True
    assert result["min_score_required"] == 82
    assert result["trade_allowed"] is True


def test_normalization_does_not_qualify_weak_three_confirmation_setup():
    result = calculate_tqu_score(
        base_score=33,
        adx=32.4,
        volume_ratio=0.0,
        mtf_confirmed=True,
        is_sideways=False,
        volume_available=False,
    )

    assert result["pre_normalization_score"] == 65
    assert result["score"] == 71
    assert result["trade_allowed"] is False


def test_available_volume_scoring_is_unchanged():
    result = calculate_tqu_score(
        base_score=44,
        adx=32.4,
        volume_ratio=0.0,
        mtf_confirmed=True,
        is_sideways=False,
        volume_available=True,
    )

    assert result["pre_normalization_score"] == 69
    assert result["score"] == 69
    assert result["availability_adjustment"] == 0
    assert result["availability_normalized"] is False
    assert result["trade_allowed"] is False


def test_full_signal_and_breakdown_show_same_normalized_decision_score():
    market = _four_of_five_pe_market()
    result = get_full_signal(market)
    payload = _score_payload(market, result)
    rows = {row["key"]: row for row in payload["components"]}

    assert result["candidate_signal"] == "PE"
    assert result["signal"] == "PE"
    assert result["score"] == 83
    assert result["trade_allowed"] is True

    assert rows["adx"]["decision_score"] == 15
    assert rows["volume"]["decision_score"] == 7
    assert rows["volume"]["max_score"] == 7
    assert rows["availability_normalization"]["decision_score"] == 7
    assert rows["availability_normalization"]["max_score"] == 8
    assert payload["decision_score"] == 83
    assert payload["decision_component_total"] == 83
    assert payload["enabled_weight_total"] == 100
    assert payload["availability_normalized"] is True


def test_missing_volume_score_is_never_normalized_twice_by_entry_guards():
    market = {
        "price": 24450.25,
        "vwap": 24459.12,
        "ema9": 24450.36,
        "ema21": 24451.50,
        "adx": 10.0,
        "volume_ratio": 0.0,
        "volume_available": False,
        "vwap_fallback_used": True,
        "supertrend_dir": "DOWN",
        "trend": "DOWNTREND",
        "mtf_confirmed": False,
        "orb_high": 24576.85,
        "orb_low": 24478.60,
        "c1_bullish": False,
        "c2_bullish": False,
        "gap_day": False,
        "atr": 10.0,
    }

    canonical = get_full_signal(market)
    fresh = _apply_fresh_entry_guard(canonical, market, None)
    fresh["mandatory_confirmations_passed"] = True
    guarded = _apply_entry_quality_v2(fresh, market, None)
    payload = _score_payload(market, guarded)

    assert canonical["score"] == 67
    assert fresh["score"] == 67
    assert guarded["score"] == 67
    assert guarded["volume_normalization_corrected"] is False
    assert guarded["volume_normalization_owner"] == "TQU_CANONICAL_V1"
    assert payload["score"] == 67
    assert payload["display_score"] == 67
    assert payload["decision_score"] == 67
    assert payload["component_total"] == 67
    assert payload["decision_component_total"] == 67
    assert payload["component_score_matches_decision"] is True


def test_orb_or_momentum_is_diagnostic_not_a_hard_entry_block():
    market = {
        "price": 100.0,
        "orb_high": 120.0,
        "orb_low": 80.0,
        "c1_bullish": True,
        "c2_bullish": False,
    }
    signal = {
        "signal": "PE",
        "candidate_signal": "PE",
        "score": 89,
        "min_score": 82,
        "trade_allowed": True,
        "mandatory_confirmations_passed": True,
        "safety_gate_reasons": [],
        "fresh_entry_block_reasons": [],
        "warnings": [],
    }

    output = _apply_entry_quality_v2(signal, market, None)

    assert output["fresh_trigger_passed"] is False
    assert output["fresh_trigger_is_blocking"] is False
    assert output["fresh_trigger_required"] == "INFORMATIONAL_ONLY"
    assert output["trade_allowed"] is True
    assert output["signal"] == "PE"
    assert "ORB_OR_MOMENTUM_TRIGGER_REQUIRED" not in output[
        "safety_gate_reasons"
    ]
    assert "ORB_OR_MOMENTUM_TRIGGER_REQUIRED" not in output[
        "fresh_entry_block_reasons"
    ]
