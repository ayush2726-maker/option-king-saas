from datetime import datetime

import pandas as pd
from zoneinfo import ZoneInfo

from bot.market_regime_shield_v2 import (
    ALIGNMENT_BLOCK_REASON,
    FRESH_BREAK_BLOCK_REASON,
    GAP_BLOCK_REASON,
    _apply_shield,
)


IST = ZoneInfo("Asia/Kolkata")


def _frame(*, breakout=True, opening=100.0):
    opens = [opening, 100.0, 100.1, 100.0, 100.2, 100.1, 100.2, 100.4, 101.0, 102.0]
    closes = [100.0, 100.1, 100.0, 100.2, 100.1, 100.2, 100.4, 101.0, 102.0, 102.1]
    if not breakout:
        opens[-3], closes[-3] = 100.2, 100.3
        opens[-2], closes[-2] = 100.3, 100.4
    return pd.DataFrame(
        {
            "time": pd.date_range(
                "2026-09-02 03:45:00+00:00", periods=10, freq="1min"
            ),
            "open": opens,
            "close": closes,
            "high": [max(o, c) + 0.1 for o, c in zip(opens, closes)],
            "low": [min(o, c) - 0.1 for o, c in zip(opens, closes)],
            "VWAP": [100.0] * 10,
        }
    )


def _scan(*, aligned=True):
    return {
        "status": "OK",
        "underlying": "NIFTY",
        "candle_id": "2026-09-02T04:10:00Z",
        "signal_data": {
            "signal": "CE",
            "candidate_signal": "CE",
            "trade_allowed": True,
            "safety_gate_reasons": [],
            "real_mtf_5m": {"available": True, "side": "CE"},
        },
        "market_data": {
            "price": 102.0,
            "vwap": 100.0,
            "ema9": 101.0 if aligned else 99.0,
            "ema21": 100.0,
            "supertrend_dir": "UP",
            "atr": 1.0,
        },
        "execution_allowed": True,
    }


def test_large_gap_waits_until_0945():
    result = _apply_shield(
        _scan(),
        _frame(opening=100.7),
        1,
        "NIFTY",
        previous_close=100.0,
        now_ist=datetime(2026, 9, 2, 9, 40, tzinfo=IST),
    )

    assert result["signal_data"]["trade_allowed"] is False
    assert result["execution_block_reason"] == GAP_BLOCK_REASON
    assert result["market_data"]["gap_day"] is True


def test_aligned_fresh_breakout_allowed_after_gap_wait():
    result = _apply_shield(
        _scan(),
        _frame(opening=100.7),
        1,
        "NIFTY",
        previous_close=100.0,
        now_ist=datetime(2026, 9, 2, 9, 46, tzinfo=IST),
    )

    assert result["signal_data"]["trade_allowed"] is True
    assert result["market_regime_shield"]["alignment"]["passed"] is True
    assert result["market_regime_shield"]["fresh_breakout"]["passed"] is True


def test_mtf_vwap_supertrend_ema_alignment_is_final_gate():
    result = _apply_shield(
        _scan(aligned=False),
        _frame(),
        1,
        "NIFTY",
        previous_close=100.0,
        now_ist=datetime(2026, 9, 2, 10, 0, tzinfo=IST),
    )

    assert result["execution_block_reason"] == ALIGNMENT_BLOCK_REASON


def test_entry_requires_fresh_two_candle_swing_break():
    result = _apply_shield(
        _scan(),
        _frame(breakout=False),
        1,
        "NIFTY",
        previous_close=100.0,
        now_ist=datetime(2026, 9, 2, 10, 0, tzinfo=IST),
    )

    assert result["execution_block_reason"] == FRESH_BREAK_BLOCK_REASON
