from bot import entry_execution_safety_v1_patch as patch


def test_first_quote_can_pass_when_option_candles_confirm_entry():
    raw = {
        "allowed": False,
        "reason": "OPTION_PREMIUM_MOMENTUM_WARMUP",
        "current_price": 100.0,
    }
    result = patch._balanced_momentum_policy(
        raw,
        {"allowed": True, "reason": "OPTION_PREMIUM_ENTRY_OK"},
    )
    assert result["allowed"] is True
    assert result["reason"] == "OPTION_PREMIUM_CANDLE_CONFIRMED"


def test_small_positive_move_is_not_hard_blocked():
    raw = {
        "allowed": False,
        "reason": "OPTION_PREMIUM_MOMENTUM_WEAK",
        "move_points": 0.10,
    }
    result = patch._balanced_momentum_policy(
        raw,
        {"allowed": True, "reason": "OPTION_PREMIUM_ENTRY_OK"},
    )
    assert result["allowed"] is True
    assert result["reason"] == "OPTION_PREMIUM_NOT_FALLING"


def test_falling_premium_stays_blocked():
    raw = {
        "allowed": False,
        "reason": "OPTION_PREMIUM_MOMENTUM_WEAK",
        "move_points": -0.10,
    }
    result = patch._balanced_momentum_policy(
        raw,
        {"allowed": True, "reason": "OPTION_PREMIUM_ENTRY_OK"},
    )
    assert result["allowed"] is False


def test_warmup_without_option_candles_still_waits():
    raw = {
        "allowed": False,
        "reason": "OPTION_PREMIUM_MOMENTUM_WARMUP",
    }
    result = patch._balanced_momentum_policy(
        raw,
        {"allowed": True, "reason": "OPTION_GUARD_FETCH_WARNING"},
    )
    assert result["allowed"] is False
