from bot.fresh_entry_guard_patch import _apply_fresh_entry_guard


def _market():
    return {
        "price": 120.0,
        "vwap": 100.0,
        "ema9": 110.0,
        "ema21": 105.0,
        "supertrend_dir": "UP",
        "trend": "UPTREND",
        "orb_high": 110.0,
        "orb_low": 90.0,
        "c1_bullish": True,
        "c2_bullish": True,
        "atr": 10.0,
        "volume_ratio": 1.3,
        "vwap_fallback_used": False,
    }


def _base_result(**updates):
    result = {
        "signal": "CE",
        "candidate_signal": "CE",
        "score": 88,
        "min_score": 82,
        "trade_allowed": True,
        "chase_blocked": False,
        "ema_chase_blocked": False,
        "vwap_chase_blocked": False,
        "sideways_blocked": False,
        "warnings": [],
    }
    result.update(updates)
    return result


def test_fresh_guard_does_not_add_old_095_atr_ema_block():
    output = _apply_fresh_entry_guard(
        _base_result(),
        _market(),
        None,
    )

    assert output["ema_distance_atr"] == 1.0
    assert "EMA_EXTENSION_OVER_0.95_ATR" not in output[
        "fresh_entry_block_reasons"
    ]
    assert output["trade_allowed"] is True
    assert output["signal"] == "CE"
    assert output["fresh_guard_uses_shared_anti_chase"] is True


def test_shared_strategy_antichase_still_blocks_trade():
    output = _apply_fresh_entry_guard(
        _base_result(
            signal="WAIT",
            trade_allowed=False,
            chase_blocked=True,
            ema_chase_blocked=True,
            warnings=["ANTI_CHASE_EMA_STRETCH:30.0pt>22.0pt"],
        ),
        _market(),
        None,
    )

    assert output["trade_allowed"] is False
    assert output["signal"] == "WAIT"
    assert output["fresh_entry_block_reasons"] == []


def test_orb_exhaustion_remains_protected():
    market = _market()
    market["price"] = 130.0
    market["ema9"] = 125.0
    market["vwap"] = 115.0

    output = _apply_fresh_entry_guard(
        _base_result(),
        market,
        None,
    )

    assert "ORB_EXTENSION_OVER_1.35_ATR" in output[
        "fresh_entry_block_reasons"
    ]
    assert output["trade_allowed"] is False
