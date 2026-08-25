from bot.orb_gap_neutral_scoring_patch import calculate_base_score_orb_neutral


def test_far_orb_does_not_cost_one_of_five_base_points():
    result = calculate_base_score_orb_neutral(
        price=24570,
        vwap=24590,
        ema9=24580,
        ema21=24595,
        supertrend_dir="DOWN",
        trend="DOWNTREND",
        orb_high=24650,
        orb_low=24620,
        c1_bullish=False,
        c2_bullish=False,
        gap_day=False,
        spot_atr=20,
    )

    assert result["orb_available"] is True
    assert result["orb_applicable"] is False
    assert result["orb_score_denominator"] == 4
    assert "ORB_NOT_APPLICABLE_FAR" in result["orb_neutral_reasons"]


def test_near_orb_keeps_normal_five_factor_scoring():
    result = calculate_base_score_orb_neutral(
        price=24610,
        vwap=24620,
        ema9=24615,
        ema21=24625,
        supertrend_dir="DOWN",
        trend="DOWNTREND",
        orb_high=24650,
        orb_low=24620,
        c1_bullish=False,
        c2_bullish=False,
        gap_day=True,
        spot_atr=20,
    )

    assert result["orb_available"] is True
    assert result["orb_applicable"] is True
    assert result["orb_score_denominator"] == 5


def test_far_opposite_orb_is_not_removed_from_countertrend_score():
    result = calculate_base_score_orb_neutral(
        price=24620,
        vwap=24630,
        ema9=24610,
        ema21=24618,
        supertrend_dir="DOWN",
        trend="DOWNTREND",
        orb_high=24550,
        orb_low=24480,
        c1_bullish=False,
        c2_bullish=False,
        gap_day=False,
        spot_atr=14,
    )

    assert result["signal"] == "PE"
    assert result["base_score"] == 44
    assert result["orb_applicable"] is True
    assert result["orb_score_denominator"] == 5
    assert result["orb_conflicts_with_active_side"] is True
    assert "ORB_FAR_OPPOSITE_DIRECTION_RETAINED" in result["orb_neutral_reasons"]
