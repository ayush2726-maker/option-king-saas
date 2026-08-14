from bot.qualified_entry_release_patch import _repair_scan, _repair_signal


def _blocked(*reasons, score=88):
    return {
        "signal": "WAIT",
        "candidate_signal": "PE",
        "score": score,
        "min_score": 82,
        "trade_allowed": False,
        "safety_gate_reasons": list(reasons),
        "fresh_entry_block_reasons": [],
        "warnings": [],
    }


def test_score_qualified_duplicate_direction_and_reversal_reasons_release():
    result = _repair_signal(
        _blocked(
            "VWAP_DIRECTION_REQUIRED",
            "SUPERTREND_DIRECTION_REQUIRED",
            "EMA_TREND_REQUIRED",
            "REVERSAL_CANDLE_AT_ENTRY",
            "REAL_5M_MTF_NOT_CONFIRMED:CE",
        )
    )

    assert result["trade_allowed"] is True
    assert result["signal"] == "PE"
    assert result["safety_gate_reasons"] == []
    assert result["qualified_entry_release_applied"] is True
    assert result["mandatory_confirmations_blocking"] is False
    assert result["reversal_candle_blocking"] is False
    assert result["mtf_confirmation_blocking"] is False


def test_score_below_82_never_releases():
    result = _repair_signal(_blocked("SCORE_BELOW_82", score=81))

    assert result["trade_allowed"] is False
    assert result["signal"] == "WAIT"
    assert result["safety_gate_reasons"] == ["SCORE_BELOW_82"]


def test_orb_extension_and_late_exhaustion_remain_blocking():
    result = _repair_signal(
        _blocked(
            "EMA_TREND_REQUIRED",
            "ORB_EXTENSION_OVER_1.35_ATR",
            "LATE_TWO_CANDLE_EXHAUSTION",
        )
    )

    assert result["trade_allowed"] is False
    assert result["signal"] == "WAIT"
    assert result["safety_gate_reasons"] == [
        "ORB_EXTENSION_OVER_1.35_ATR",
        "LATE_TWO_CANDLE_EXHAUSTION",
    ]


def test_session_countertrend_and_sideways_protection_remain_blocking():
    signal = _blocked(
        "VWAP_DIRECTION_REQUIRED",
        "SESSION_COUNTER_TREND_BLOCKED:CE",
    )
    signal["session_counter_trend_blocked"] = True
    signal["sideways_blocked"] = True

    result = _repair_signal(signal)

    assert result["trade_allowed"] is False
    assert result["safety_gate_reasons"] == ["SESSION_COUNTER_TREND_BLOCKED:CE"]


def test_unexplained_wait_is_not_converted_into_a_trade():
    result = _repair_signal(_blocked())

    assert result["trade_allowed"] is False
    assert result["qualified_entry_release_applied"] is False


def test_released_scan_updates_execution_payload():
    scan = {
        "status": "OK",
        "signal_data": _blocked("REVERSAL_CANDLE_AT_ENTRY"),
        "market_data": {"signal": "WAIT", "execution_allowed": False},
        "execution_allowed": False,
        "execution_block_reason": "REVERSAL_CANDLE_AT_ENTRY",
    }

    result = _repair_scan(scan)

    assert result["signal_data"]["trade_allowed"] is True
    assert result["signal_data"]["execution_allowed"] is True
    assert result["market_data"]["signal"] == "PE"
    assert result["execution_allowed"] is True
    assert result["execution_block_reason"] == ""
