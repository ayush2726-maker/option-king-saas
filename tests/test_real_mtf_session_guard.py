from datetime import datetime

import pandas as pd

from bot import paper_market_close_1530_patch as paper_cutoff
from bot.real_mtf_session_guard_patch import (
    _completed_5m_snapshot,
    _repair_scan,
    _restore_normal_auto_cutoff,
)


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

    # Remove the old fake 10-point MTF bonus: 92 -> 82.  Even at threshold,
    # the clear bullish 5m + ORB session bias must block the PE pullback.
    assert signal["score"] == 82
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

    assert signal["score"] == 92
    assert signal["mtf_confirmed"] is True
    assert signal["session_bias"] == "CE"
    assert signal["session_counter_trend_blocked"] is False
    assert signal["trade_allowed"] is True
    assert signal["signal"] == "CE"


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
