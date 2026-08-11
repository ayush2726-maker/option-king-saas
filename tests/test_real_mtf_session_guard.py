from datetime import datetime

import pandas as pd

from bot import paper_market_close_1530_patch as paper_cutoff
from bot.mandatory_trend_structure_patch import (
    apply_mandatory_trend_structure_patch,
)
from bot.real_mtf_session_guard_patch import (
    _completed_5m_snapshot,
    _entry_window_state,
    _repair_scan,
    _restore_normal_auto_cutoff,
)

# Production installs the standard final-band Supertrend before the final real
# MTF guard.  Reproduce that exact patch order in the focused unit test.
apply_mandatory_trend_structure_patch()


def _bullish_one_minute_frame():
    times = pd.date_range(
        "2026-08-03 09:15:00+05:30",
        periods=125,
        freq="1min",
    )
    rows = []
    price = 24500.0
    for index, timestamp in enumerate(times):
        open_price = price
        # Smooth bullish session with small pullbacks.  Completed 5m EMA and ST
        # must remain bullish even if one 1m snapshot later looks bearish.
        change = 0.9 if index % 7 else -0.35
        close_price = open_price + change
        rows.append(
            {
                "time": timestamp,
                "open": open_price,
                "high": max(open_price, close_price) + 0.6,
                "low": min(open_price, close_price) - 0.6,
                "close": close_price,
                "volume": 0,
            }
        )
        price = close_price
    return pd.DataFrame(rows)


def _scan(candidate="PE", score=92):
    if candidate == "PE":
        market = {
            "price": 24620.0,
            "vwap": 24630.0,
            "ema9": 24610.0,
            "ema21": 24618.0,
            "supertrend_dir": "DOWN",
            "trend": "DOWNTREND",
            "orb_high": 24550.0,
            "orb_low": 24480.0,
        }
    else:
        market = {
            "price": 24620.0,
            "vwap": 24600.0,
            "ema9": 24618.0,
            "ema21": 24610.0,
            "supertrend_dir": "UP",
            "trend": "UPTREND",
            "orb_high": 24550.0,
            "orb_low": 24480.0,
        }

    market.update(
        {
            "adx": 30.0,
            "volume_ratio": 1.4,
            "volume_available": True,
            "mtf_confirmed": True,
            "c1_bullish": candidate == "CE",
            "c2_bullish": candidate == "CE",
            "gap_day": False,
            "atr": 14.0,
        }
    )
    return {
        "underlying": "NIFTY",
        "status": "OK",
        "candle_id": "2026-08-03T11:18:00+05:30",
        "market_data": market,
        "signal_data": {
            "signal": candidate,
            "candidate_signal": candidate,
            "score": score,
            "decision_score": score,
            "min_score": 82,
            "trade_allowed": True,
            "mtf_confirmed": True,
            "mtf_bonus": 10,
            "profile_weights": {"mtf": 10},
            "profile_enabled": {"mtf": True},
            "warnings": ["REPLAY_FIRST_LIVE_SCAN"],
            "safety_gate_reasons": [],
            "fresh_entry_block_reasons": [],
        },
    }


def test_completed_five_minute_snapshot_is_real_and_bullish():
    frame = _bullish_one_minute_frame()
    snapshot = _completed_5m_snapshot(frame, frame.iloc[-2]["time"])

    assert snapshot["available"] is True
    assert snapshot["bar_count"] >= 12
    assert snapshot["trend"] == "UPTREND"
    assert snapshot["supertrend_dir"] == "UP"
    assert snapshot["side"] == "CE"


def test_bullish_session_blocks_one_minute_pe_pullback():
    frame = _bullish_one_minute_frame()
    repaired = _repair_scan(_scan("PE", 92), frame, {})
    signal = repaired["signal_data"]

    # Recalculate from raw components with real MTF false. Never subtract 10
    # from an already-normalised replay score (the old 92 -> 82 defect).
    assert signal["score"] == 61
    assert signal["canonical_score_recomputed"] is True
    assert signal["mtf_confirmed"] is False
    assert signal["session_bias"] == "CE"
    assert signal["session_counter_trend_blocked"] is True
    assert signal["trade_allowed"] is False
    assert signal["signal"] == "WAIT"
    assert "SESSION_COUNTER_TREND_BLOCKED:CE" in signal["safety_gate_reasons"]


def test_matching_ce_keeps_real_mtf_bonus():
    frame = _bullish_one_minute_frame()
    repaired = _repair_scan(_scan("CE", 92), frame, {})
    signal = repaired["signal_data"]

    # 55 directional + 14 ADX + 3 volume + 10 real MTF = 82.
    assert signal["score"] == 82
    assert signal["mtf_confirmed"] is True
    assert signal["session_bias"] == "CE"
    assert signal["session_counter_trend_blocked"] is False
    assert signal["trade_allowed"] is True
    assert signal["signal"] == "CE"


def test_screenshot_setup_recalculates_to_67_instead_of_inflated_82(monkeypatch):
    from bot import real_mtf_session_guard_patch as guard

    market = {
        "price": 24450.25,
        "vwap": 24459.12,
        "ema9": 24450.36,
        "ema21": 24451.50,
        "supertrend_dir": "DOWN",
        "trend": "DOWNTREND",
        "orb_high": 24576.85,
        "orb_low": 24478.60,
        "adx": 10.0,
        "volume_ratio": 0.0,
        "volume_available": False,
        "vwap_fallback_used": True,
        "mtf_confirmed": True,
        "c1_bullish": False,
        "c2_bullish": False,
        "gap_day": False,
        "atr": 10.0,
    }
    scan = {
        "underlying": "NIFTY",
        "status": "OK",
        "candle_id": "2026-08-11T15:29:00+05:30",
        "market_data": market,
        "signal_data": {
            "signal": "PE",
            "candidate_signal": "PE",
            "score": 82,
            "decision_score": 82,
            "min_score": 82,
            "trade_allowed": True,
            "mtf_confirmed": True,
            "mtf_bonus": 10,
            "warnings": ["REPLAY_FIRST_LIVE_SCAN"],
        },
        "chart_candles": [
            {"time": "2026-08-11T15:29:00+05:30", "score": 82}
        ],
    }
    monkeypatch.setattr(
        guard,
        "_completed_5m_snapshot",
        lambda *_args, **_kwargs: {
            "available": True,
            "reason": "REAL_5M_OK",
            "side": "CE",
            "trend": "UPTREND",
            "supertrend_dir": "UP",
            "bar_count": 20,
            "candle_time": "2026-08-11T15:25:00+05:30",
        },
    )

    repaired = guard._repair_scan(scan, frame=None, profile={})
    signal = repaired["signal_data"]

    assert signal["candidate_signal"] == "PE"
    assert signal["score"] == 67
    assert signal["decision_score"] == 67
    assert signal["pre_normalization_score"] == 62
    assert signal["availability_adjustment"] == 5
    assert signal["mtf_bonus"] == 0
    assert signal["trade_allowed"] is False
    assert repaired["chart_candles"][-1]["score"] == 67


def test_normal_auto_paper_cutoff_is_1445_and_eod_stays_1525():
    _restore_normal_auto_cutoff()

    before_cutoff = datetime(2026, 8, 3, 14, 44)
    after_cutoff = datetime(2026, 8, 3, 15, 5)

    with paper_cutoff._entry_mode("paper"):
        assert paper_cutoff._entry_window_open(before_cutoff) is True
        assert paper_cutoff._entry_window_open(after_cutoff) is False
        assert paper_cutoff._entry_block_reason(after_cutoff) == "AUTO_ENTRY_CUTOFF_1445_IST"

    assert paper_cutoff.PAPER_ENTRY_CUTOFF_MINUTE == 14 * 60 + 45
    assert paper_cutoff.PAPER_EOD_MINUTE == 15 * 60 + 25


def test_entry_window_status_distinguishes_cutoff_and_market_close():
    assert _entry_window_state(datetime(2026, 8, 11, 14, 44))["open"] is True
    assert _entry_window_state(datetime(2026, 8, 11, 14, 45))["reason"] == (
        "AUTO_ENTRY_CUTOFF_1445_IST"
    )
    assert _entry_window_state(datetime(2026, 8, 11, 15, 30))["reason"] == (
        "MARKET_CLOSED_AFTER_1530_IST"
    )
