from bot.live_score_breakdown_patch import _score_payload
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

