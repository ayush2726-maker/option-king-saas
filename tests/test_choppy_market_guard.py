import pandas as pd

from bot.regime_accuracy_confirmation_patch import _choppy_assessment
from bot.choppy_market_guard_patch import _apply_guard, apply_choppy_market_guard_patch


def _frame(*, breakout=False):
    opens = [100.0, 100.2, 99.9, 100.25, 99.95, 100.2, 99.9, 100.0, 100.1]
    closes = [100.2, 99.9, 100.25, 99.95, 100.2, 99.9, 100.15, 100.2, 100.0]
    if breakout:
        opens[6], closes[6] = 99.9, 100.4
        opens[7], closes[7] = 100.3, 102.0
    return pd.DataFrame({
        "open": opens,
        "close": closes,
        "high": [max(o, c) + 0.2 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.2 for o, c in zip(opens, closes)],
    })


def _market(**changes):
    result = {
        "adx": 16.0,
        "atr": 4.0,
        "price": 100.2,
        "ema9": 100.1,
        "ema21": 100.0,
        "vwap": 100.0,
        "vwap_fallback_used": False,
        "mtf_confirmed": True,
    }
    result.update(changes)
    return result


def test_low_adx_congestion_and_candle_flips_block_choppy_entry():
    result = _choppy_assessment(_frame(), _market(), "CE", 90, 82)

    assert result["choppy"] is True
    assert result["repeated_body_flips"] is True
    assert result["hard_block"] is True
    assert result["strong_breakout_override"] is False


def test_score_88_two_candle_breakout_is_allowed_through_choppy_guard():
    result = _choppy_assessment(
        _frame(breakout=True),
        _market(price=102.0, adx=20.0),
        "CE",
        90,
        82,
    )

    assert result["choppy"] is True
    assert result["breakout"] is True
    assert result["two_candle_momentum"] is True
    assert result["strong_breakout_override"] is True
    assert result["hard_block"] is False


def test_very_low_adx_cannot_use_breakout_override():
    result = _choppy_assessment(
        _frame(breakout=True),
        _market(price=102.0, adx=16.0),
        "CE",
        90,
        82,
    )

    assert result["very_weak_adx"] is True
    assert result["strong_breakout_override"] is False
    assert result["hard_block"] is True


def test_three_real_vwap_crosses_in_45m_are_hard_blocked():
    frame = _frame()
    frame["VWAP"] = 100.0
    frame["close"] = [99.0, 101.0, 99.0, 101.0, 101.2, 101.3, 101.4, 101.5, 101.6]
    result = _choppy_assessment(
        frame,
        _market(price=101.5, adx=30.0),
        "CE",
        95,
        82,
    )

    assert result["vwap_crosses_45m"] >= 3
    assert result["repeated_vwap_crosses"] is True
    assert result["hard_block"] is True


def test_trending_adx_is_not_classified_as_choppy():
    result = _choppy_assessment(_frame(), _market(adx=30.0), "CE", 90, 82)

    assert result["weak_adx"] is False
    assert result["choppy"] is False
    assert result["hard_block"] is False


def _scan(*, breakout=False, mtf_side="CE"):
    price = 102.0 if breakout else 100.2
    return {
        "status": "OK",
        "signal_data": {
            "signal": "CE",
            "candidate_signal": "CE",
            "score": 90,
            "min_score": 82,
            "trade_allowed": True,
            "safety_gate_reasons": [],
            "real_mtf_5m": {"available": True, "side": mtf_side},
        },
        "market_data": _market(price=price),
        "execution_allowed": True,
    }


def test_final_guard_marks_choppy_setup_wait_with_visible_reason():
    result = _apply_guard(_scan(), _frame())

    assert result["signal_data"]["signal"] == "WAIT"
    assert result["signal_data"]["trade_allowed"] is False
    assert result["signal_data"]["choppy_market_blocked"] is True
    assert "CHOPPY_RANGE_NO_BREAKOUT" in result["signal_data"]["safety_gate_reasons"]
    assert result["execution_allowed"] is False


def test_final_guard_requires_matching_real_5m_for_breakout_override():
    allowed = _apply_guard(_scan(breakout=True), _frame(breakout=True))
    blocked = _apply_guard(
        _scan(breakout=True, mtf_side="PE"),
        _frame(breakout=True),
    )

    assert allowed["signal_data"]["trade_allowed"] is True
    assert allowed["signal_data"]["choppy_market_blocked"] is False
    assert blocked["signal_data"]["trade_allowed"] is False
    assert blocked["signal_data"]["choppy_market_blocked"] is True


def test_production_replay_scan_cannot_bypass_choppy_guard(monkeypatch):
    from bot import auto_portfolio_runtime as runtime
    from bot import live_scan_history_fallback_patch as replay

    monkeypatch.delattr(runtime, "_okai_choppy_market_guard_v2", raising=False)
    monkeypatch.setattr(runtime, "_build_scan", lambda *args: _scan())
    monkeypatch.setattr(replay, "_replay_scan", lambda *args: _scan())

    assert apply_choppy_market_guard_patch() is True
    result = replay._replay_scan(1, "NIFTY", _frame(), {}, "TEST", [])

    assert result["signal_data"]["trade_allowed"] is False
    assert result["execution_block_reason"] == "CHOPPY_RANGE_NO_BREAKOUT"
