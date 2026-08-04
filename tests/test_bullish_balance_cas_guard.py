from datetime import datetime, timezone

from bot.bullish_balance_cas_guard_patch import (
    CAS_SAFE_EXIT_MINUTE,
    LEGACY_EOD_EXIT_MINUTE,
    classify_completed_candle,
    eod_exit_minute_for,
    momentum_pattern,
    momentum_score_flags,
)


def _market(**overrides):
    base = {
        "trend": "UPTREND",
        "supertrend_dir": "UP",
        "price": 101.0,
        "ema9": 100.0,
        "vwap": 99.5,
        "vwap_fallback_used": False,
    }
    base.update(overrides)
    return base


def test_doji_is_neutral_and_does_not_score_as_pe():
    first = classify_completed_candle(
        {"open": 100, "high": 102, "low": 99, "close": 100.2},
        atr=8,
    )
    second = classify_completed_candle(
        {"open": 101, "high": 103, "low": 100, "close": 101.1},
        atr=8,
    )

    assert first["direction"] == "NEUTRAL"
    assert second["direction"] == "NEUTRAL"
    pattern = momentum_pattern(first, second, _market())
    assert pattern == "NO_MOMENTUM"
    assert momentum_score_flags(pattern) == (True, False)


def test_two_clear_red_candles_keep_valid_pe_momentum():
    first = classify_completed_candle(
        {"open": 105, "high": 106, "low": 101, "close": 102},
        atr=8,
    )
    second = classify_completed_candle(
        {"open": 102, "high": 103, "low": 97, "close": 98},
        atr=8,
    )

    pattern = momentum_pattern(
        first,
        second,
        _market(
            trend="DOWNTREND",
            supertrend_dir="DOWN",
            price=98,
            ema9=100,
            vwap=101,
        ),
    )
    assert pattern == "TWO_CLEAR_BEARISH"
    assert momentum_score_flags(pattern) == (False, False)


def test_bullish_pullback_reclaim_gets_symmetric_momentum_confirmation():
    first = classify_completed_candle(
        {"open": 101, "high": 102, "low": 98, "close": 99},
        atr=7,
    )
    second = classify_completed_candle(
        {"open": 99, "high": 104, "low": 98.5, "close": 103},
        atr=7,
    )

    pattern = momentum_pattern(first, second, _market(price=103, ema9=101, vwap=100))
    assert pattern == "BULLISH_PULLBACK_RECLAIM"
    assert momentum_score_flags(pattern) == (True, True)


def test_bearish_pullback_rejection_is_symmetric():
    first = classify_completed_candle(
        {"open": 99, "high": 103, "low": 98, "close": 102},
        atr=7,
    )
    second = classify_completed_candle(
        {"open": 102, "high": 102.5, "low": 96, "close": 97},
        atr=7,
    )

    pattern = momentum_pattern(
        first,
        second,
        _market(
            trend="DOWNTREND",
            supertrend_dir="DOWN",
            price=97,
            ema9=99,
            vwap=100,
        ),
    )
    assert pattern == "BEARISH_PULLBACK_REJECTION"
    assert momentum_score_flags(pattern) == (False, False)


def test_cas_exit_is_date_aware():
    before_cas = datetime(2026, 7, 31, 9, 40, tzinfo=timezone.utc)
    after_cas = datetime(2026, 8, 4, 9, 40, tzinfo=timezone.utc)

    # Values represent IST-like wall-clock timestamps passed directly to helper.
    assert eod_exit_minute_for(before_cas) == LEGACY_EOD_EXIT_MINUTE
    assert eod_exit_minute_for(after_cas) == CAS_SAFE_EXIT_MINUTE
