from datetime import datetime, timedelta, timezone

from bot import pullback_continuation_entry_patch as pullback


NOW = datetime(2026, 8, 12, 5, 30, tzinfo=timezone.utc)


def _market(**updates):
    market = {
        "price": 24297.55,
        "vwap": 24388.74,
        "ema9": 24320.61,
        "ema21": 24340.25,
        "adx": 77.6,
        "atr": 15.0,
        "supertrend_dir": "DOWN",
        "trend": "DOWNTREND",
        "orb_high": 24473.30,
        "orb_low": 24390.60,
        "c1_bullish": False,
        "c2_bullish": False,
    }
    market.update(updates)
    return market


def _signal(**updates):
    signal = {
        "signal": "WAIT",
        "candidate_signal": "PE",
        "score": 100,
        "min_score": 82,
        "trade_allowed": False,
        "strategy_profile_key": "okai_default_82",
        "mtf_confirmed": True,
        "real_mtf_5m": {"available": True, "side": "PE"},
        "entry_window_open": True,
        "ema_chase_blocked": True,
        "vwap_chase_blocked": False,
        "chase_blocked": True,
        "sideways_blocked": False,
        "session_counter_trend_blocked": False,
        "safety_gate_reasons": [
            "ORB_EXTENSION_OVER_1.35_ATR",
            "LATE_TWO_CANDLE_EXHAUSTION",
            "EMA_ANTI_CHASE:23.1>22.0",
            "EMA_ANTI_CHASE",
        ],
        "fresh_entry_block_reasons": [
            "ORB_EXTENSION_OVER_1.35_ATR",
            "LATE_TWO_CANDLE_EXHAUSTION",
        ],
        "warnings": [
            "FRESH_ENTRY_BLOCK:ORB_EXTENSION_OVER_1.35_ATR",
            "FRESH_ENTRY_BLOCK:LATE_TWO_CANDLE_EXHAUSTION",
        ],
    }
    signal.update(updates)
    return signal


def _state(status=pullback.STATE_ARMED, **updates):
    state = {
        "id": 7,
        "user_id": 1,
        "underlying": "NIFTY",
        "side": "PE",
        "status": status,
        "armed_at": pullback._iso(NOW),
        "armed_candle_id": "2026-08-12 10:59:00+05:30",
        "armed_price": 24297.55,
        "armed_score": 100,
        "pullback_at": None,
        "pullback_candle_id": None,
        "pullback_price": None,
        "ready_at": None,
        "ready_candle_id": None,
        "expires_at": pullback._iso(NOW + timedelta(minutes=45)),
        "last_candle_id": "2026-08-12 10:59:00+05:30",
        "updated_at": pullback._iso(NOW),
        "version": pullback.PATCH_VERSION,
    }
    state.update(updates)
    return state


def test_exact_extended_100_score_setup_is_armable_not_chased():
    signal = _signal()
    market = _market()

    assert pullback._armable_setup(signal, market) is True

    scan = {"signal_data": signal, "market_data": market}
    annotated = pullback._annotate_waiting(
        scan,
        _state(),
        "PULLBACK_ENTRY_ARMED_WAITING_FOR_EMA",
    )
    assert annotated["signal_data"]["trade_allowed"] is False
    assert annotated["signal_data"]["pullback_entry_state"] == "ARMED"


def test_arm_never_weakens_threshold_session_or_profile_rules():
    market = _market()

    assert pullback._armable_setup(_signal(score=81), market) is False
    assert pullback._armable_setup(
        _signal(entry_window_open=False),
        market,
    ) is False
    assert pullback._armable_setup(
        _signal(),
        market,
        {"profile_key": "custom_aggressive"},
    ) is False


def test_unrelated_safety_block_cannot_be_armed():
    signal = _signal(
        safety_gate_reasons=[
            "ORB_EXTENSION_OVER_1.35_ATR",
            "SESSION_COUNTER_TREND_BLOCKED:CE",
        ],
        session_counter_trend_blocked=True,
    )

    assert pullback._armable_setup(signal, _market()) is False


def test_countertrend_candle_near_ema_marks_pullback():
    market = _market(
        price=24317.0,
        ema9=24320.0,
        c1_bullish=False,
        c2_bullish=True,
    )
    candle = {
        "open": 24308.0,
        "high": 24322.0,
        "low": 24306.0,
        "close": 24317.0,
        "bullish": True,
    }

    assert pullback._pullback_seen("PE", market, candle) is True


def test_next_bearish_continuation_is_ready_only_inside_current_antichase():
    state = _state(
        status=pullback.STATE_PULLBACK,
        pullback_at=pullback._iso(NOW - timedelta(minutes=1)),
        pullback_candle_id="2026-08-12 11:00:00+05:30",
        pullback_price=24317.0,
    )
    market = _market(
        price=24310.0,
        ema9=24318.0,
        ema21=24339.0,
        atr=15.0,
        c1_bullish=True,
        c2_bullish=False,
    )
    signal = _signal(
        score=88,
        ema_chase_blocked=False,
        chase_blocked=False,
        safety_gate_reasons=["ORB_EXTENSION_OVER_1.35_ATR"],
        fresh_entry_block_reasons=["ORB_EXTENSION_OVER_1.35_ATR"],
    )
    candles = {
        "previous": {
            "id": "2026-08-12 11:00:00+05:30",
            "bullish": True,
        },
        "current": {
            "id": "2026-08-12 11:01:00+05:30",
            "open": 24317.0,
            "high": 24318.0,
            "low": 24308.0,
            "close": 24310.0,
            "bullish": False,
        },
    }

    assert pullback._continuation_ready(state, signal, market, candles, NOW) is True

    scan = {"signal_data": signal, "market_data": market, "chart_candles": []}
    released = pullback._release_continuation(scan, state)
    final = released["signal_data"]
    assert final["score"] == 88
    assert final["trade_allowed"] is True
    assert final["signal"] == "PE"
    assert final["mtf_confirmed"] is True
    assert final["pullback_entry_ready"] is True
    assert "ORB_EXTENSION_OVER_1.35_ATR" not in final["safety_gate_reasons"]


def test_cached_scan_does_not_miss_first_safe_continuation():
    state = _state(
        status=pullback.STATE_PULLBACK,
        pullback_at=pullback._iso(NOW - timedelta(minutes=2)),
        pullback_candle_id="2026-08-12 11:00:00+05:30",
        pullback_price=24317.0,
    )
    market = _market(
        price=24311.0,
        ema9=24318.0,
        atr=15.0,
        c1_bullish=False,
        c2_bullish=False,
    )
    signal = _signal(
        score=88,
        ema_chase_blocked=False,
        chase_blocked=False,
        safety_gate_reasons=["ORB_EXTENSION_OVER_1.35_ATR"],
        fresh_entry_block_reasons=["ORB_EXTENSION_OVER_1.35_ATR"],
    )
    candles = {
        "previous": {
            "id": "2026-08-12 11:01:00+05:30",
            "bullish": False,
        },
        "current": {
            "id": "2026-08-12 11:02:00+05:30",
            "bullish": False,
            "close": 24311.0,
        },
    }

    assert pullback._continuation_ready(state, signal, market, candles, NOW) is True


def test_continuation_does_not_bypass_current_ema_antichase():
    state = _state(
        status=pullback.STATE_PULLBACK,
        pullback_at=pullback._iso(NOW - timedelta(minutes=1)),
        pullback_candle_id="2026-08-12 11:00:00+05:30",
        pullback_price=24317.0,
    )
    market = _market(
        price=24290.0,
        ema9=24318.0,
        atr=15.0,
        c1_bullish=True,
        c2_bullish=False,
    )
    signal = _signal(score=100, ema_chase_blocked=True, chase_blocked=True)
    candles = {
        "previous": {"id": state["pullback_candle_id"], "bullish": True},
        "current": {"id": "next", "bullish": False, "close": 24290.0},
    }

    assert pullback._continuation_ready(state, signal, market, candles, NOW) is False


def test_mtf_flip_never_releases_pullback_entry():
    state = _state(
        status=pullback.STATE_PULLBACK,
        pullback_at=pullback._iso(NOW - timedelta(minutes=1)),
        pullback_candle_id="pb",
        pullback_price=24317.0,
    )
    market = _market(
        price=24310.0,
        ema9=24318.0,
        atr=15.0,
        c1_bullish=True,
        c2_bullish=False,
    )
    signal = _signal(
        score=88,
        ema_chase_blocked=False,
        chase_blocked=False,
        mtf_confirmed=False,
        real_mtf_5m={"available": True, "side": "CE"},
    )
    candles = {
        "previous": {"id": "pb", "bullish": True},
        "current": {"id": "next", "bullish": False, "close": 24310.0},
    }

    assert pullback._continuation_ready(state, signal, market, candles, NOW) is False
