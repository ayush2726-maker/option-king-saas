from backtest.post_loss_reentry_cooldown_patch import _filter_result
from bot.entry_timing_calibration_patch import _apply_timing_gate


def _allowed_signal():
    return {
        "signal": "PE",
        "candidate_signal": "PE",
        "score": 100,
        "min_score": 82,
        "trade_allowed": True,
        "safety_gate_passed": True,
        "safety_gate_reasons": [],
        "fresh_entry_block_reasons": [],
        "warnings": [],
    }


def test_score_100_is_blocked_when_entry_is_late_from_ema():
    output = _apply_timing_gate(
        _allowed_signal(),
        {
            "price": 100.0,
            "ema9": 110.0,
            "vwap": 108.0,
            "atr": 10.0,
            "vwap_fallback_used": False,
        },
    )

    assert output["score"] == 100
    assert output["trade_allowed"] is False
    assert output["signal"] == "WAIT"
    assert "EMA_EXTENSION_OVER_0.95_ATR" in output[
        "entry_timing_block_reasons"
    ]


def test_fresh_score_100_remains_allowed():
    output = _apply_timing_gate(
        _allowed_signal(),
        {
            "price": 100.0,
            "ema9": 94.0,
            "vwap": 85.0,
            "atr": 10.0,
            "vwap_fallback_used": False,
        },
    )

    assert output["trade_allowed"] is True
    assert output["signal"] == "PE"
    assert output["entry_timing_block_reasons"] == []


def test_same_side_reentry_inside_15_minutes_is_removed():
    result = {
        "success": True,
        "capital": 100000,
        "trades": [
            {
                "trade_no": 1,
                "side": "PE",
                "entry_time": "2026-06-01T10:00:00+05:30",
                "exit_time": "2026-06-01T10:05:00+05:30",
                "entry_price": 100,
                "exit_price": 90,
                "pnl": -10,
                "reason": "PURE_ATR_SL",
            },
            {
                "trade_no": 2,
                "side": "PE",
                "entry_time": "2026-06-01T10:08:00+05:30",
                "exit_time": "2026-06-01T10:12:00+05:30",
                "entry_price": 95,
                "exit_price": 85,
                "pnl": -10,
                "reason": "PURE_ATR_SL",
            },
            {
                "trade_no": 3,
                "side": "CE",
                "entry_time": "2026-06-01T10:09:00+05:30",
                "exit_time": "2026-06-01T10:14:00+05:30",
                "entry_price": 80,
                "exit_price": 85,
                "pnl": 5,
                "reason": "PROFIT_LOCK_TRAIL",
            },
            {
                "trade_no": 4,
                "side": "PE",
                "entry_time": "2026-06-01T10:21:00+05:30",
                "exit_time": "2026-06-01T10:25:00+05:30",
                "entry_price": 90,
                "exit_price": 95,
                "pnl": 5,
                "reason": "PROFIT_LOCK_TRAIL",
            },
        ],
    }

    output = _filter_result(result)

    assert output["total_trades"] == 3
    assert output["post_loss_reentries_blocked"] == 1
    assert [row["side"] for row in output["trades"]] == ["PE", "CE", "PE"]
    assert output["total_pnl"] == 0
