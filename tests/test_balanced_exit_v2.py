from bot.balanced_exit_v2_patch import (
    _balanced_ladder,
    _post_loss_required_score,
    calculate_balanced_atr_levels,
)
from bot.structural_exit_v2_patch import structural_flip


def _true_be(price=102.0):
    return {
        "price": price,
        "target_net_profit": 40.0,
        "net_pnl_at_price": 40.0,
        "total_charges_at_price": 48.0,
        "slippage_cost_at_price": 4.0,
        "quantity_basis": 20,
        "instrument_basis": "NIFTY",
        "broker_basis": "angelone",
        "trading_mode_basis": "paper",
    }


def test_normal_stop_is_capped_at_six_percent():
    levels = calculate_balanced_atr_levels(
        spot_price=25000,
        option_entry_price=100,
        spot_atr=100,
        is_expiry_day=False,
    )
    assert levels["risk_points"] == 6.0
    assert levels["sl_price"] == 94.0
    assert levels["hard_premium_risk_cap_percent"] == 6.0


def test_expiry_stop_remains_capped_at_eight_percent():
    levels = calculate_balanced_atr_levels(
        spot_price=25000,
        option_entry_price=100,
        spot_atr=100,
        is_expiry_day=True,
    )
    assert levels["risk_points"] == 8.0
    assert levels["sl_price"] == 92.0
    assert levels["hard_premium_risk_cap_percent"] == 8.0


def test_true_be_waits_until_point_eight_r():
    waiting = _balanced_ladder(
        entry_price=100,
        initial_risk=10,
        current_sl=94,
        peak_price=107,
        current_price=107,
        true_be=_true_be(),
        mode_label="TEST",
    )
    triggered = _balanced_ladder(
        entry_price=100,
        initial_risk=10,
        current_sl=94,
        peak_price=108.1,
        current_price=108.1,
        true_be=_true_be(),
        mode_label="TEST",
    )
    assert waiting["breakeven_triggered"] is False
    assert waiting["sl_price"] == 94.0
    assert triggered["breakeven_triggered"] is True
    assert triggered["sl_price"] >= 102.0


def test_profit_ladder_locks_more_as_peak_expands():
    at_1_2r = _balanced_ladder(100, 10, 94, 112, 112, _true_be(), "TEST")
    at_1_8r = _balanced_ladder(100, 10, 94, 118, 118, _true_be(), "TEST")
    at_2_5r = _balanced_ladder(100, 10, 94, 125, 125, _true_be(), "TEST")
    assert at_1_2r["sl_price"] >= 106.0
    assert at_1_8r["sl_price"] >= 111.0
    assert at_2_5r["sl_price"] >= 119.0


def test_post_loss_quality_thresholds():
    assert _post_loss_required_score(0) == 82
    assert _post_loss_required_score(1) == 85
    assert _post_loss_required_score(2) == 88
    assert _post_loss_required_score(5) == 88


def test_two_of_three_weakness_is_detected_for_ce():
    state = structural_flip(
        "CE",
        {
            "price": 99,
            "vwap": 100,
            "ema9": 101,
            "ema21": 100,
            "supertrend_dir": "DOWN",
        },
    )
    assert state["opposite_count"] == 2
    assert state["all_three_flipped"] is False
