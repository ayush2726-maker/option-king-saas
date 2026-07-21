import math

from backtest import routes
from backtest.cost_idempotence_patch import apply_cost_idempotence_patch
from backtest.realism_costs_patch import calculate_option_round_trip_costs
from backtest import cost_safe_breakeven_risk_patch as true_be
from backtest.all_in_risk_cap_patch import apply_all_in_risk_cap_patch


def test_raw_entry_is_not_true_breakeven_after_costs():
    entry = 132.52
    qty = 540
    raw = calculate_option_round_trip_costs(
        "angelone",
        "SENSEX",
        entry,
        entry,
        qty,
    )
    assert raw["net_pnl"] < 0

    solved = true_be.calculate_cost_safe_breakeven_price(
        "angelone",
        "SENSEX",
        entry,
        qty,
        2.0,
    )
    assert solved["price"] > entry
    assert solved["net_pnl_at_price"] >= solved["target_net_profit"]
    assert solved["target_net_profit"] == round(entry * qty * 0.02, 2)


def test_first_trail_waits_for_costs_plus_two_percent():
    entry = 100.0
    risk = 20.0
    initial_sl = 80.0
    solved = true_be.calculate_cost_safe_breakeven_price(
        "angelone",
        "SENSEX",
        entry,
        20,
        2.0,
    )
    be_price = solved["price"]

    with true_be._trail_context("angelone", "SENSEX", 20):
        waiting = true_be._cost_safe_profit_lock(
            entry,
            risk,
            initial_sl,
            be_price,
            be_price,
        )
        locked = true_be._cost_safe_profit_lock(
            entry,
            risk,
            initial_sl,
            be_price + 0.05,
            be_price + 0.05,
        )

    assert waiting["breakeven_triggered"] is False
    assert waiting["sl_price"] == initial_sl
    assert locked["breakeven_triggered"] is True
    assert locked["sl_price"] >= be_price
    assert "TRUE_BE" in locked["stage"] or "2PCT" in locked["stage"]


def _oversized_sensex_trade(reason, exit_price):
    return {
        "instrument": "SENSEX",
        "entry_price": 500.0,
        "exit_price": exit_price,
        "qty": 160,
        "lots": 8,
        "lot_size": 20,
        "risk_points": 125.0,
        "sl_price": exit_price,
        "reason": reason,
        "peak_price": 500.0,
    }


def _auto_result():
    return {
        "success": True,
        "instrument": "AUTO",
        "capital": 100000.0,
    }


def test_all_in_risk_cap_reduces_eight_sensex_lots_to_one():
    apply_all_in_risk_cap_patch()
    capped = true_be._risk_capped_trade(
        _oversized_sensex_trade("PURE_ATR_SL", 375.0),
        _auto_result(),
        "angelone",
        100000.0,
    )

    assert not capped.get("risk_cap_skipped", False)
    assert capped["lots"] == 1
    assert capped["qty"] == 20
    assert math.isclose(capped["exit_price"], 460.0, abs_tol=0.01)
    assert capped["expected_max_loss_after_all_costs"] <= 1000.0
    assert capped["max_loss_rupees"] == 1000.0
    assert capped["pnl"] >= -1000.0


def test_hard_stop_precedes_reversal_or_day_end_below_stop():
    apply_all_in_risk_cap_patch()
    capped = true_be._risk_capped_trade(
        _oversized_sensex_trade("TWO_CANDLE_REVERSAL_EXIT", 300.0),
        _auto_result(),
        "angelone",
        100000.0,
    )

    assert not capped.get("risk_cap_skipped", False)
    assert capped["reason"] == "HARD_RISK_CAP_SL"
    assert capped["hard_stop_precedence_applied"] is True
    assert math.isclose(capped["exit_price"], 460.0, abs_tol=0.01)
    assert capped["expected_max_loss_after_all_costs"] <= 1000.0
    assert capped["pnl"] >= -1000.0


def test_activation_replaces_route_trail_and_enables_all_in_cap():
    apply_cost_idempotence_patch()
    assert routes.update_option_profit_lock is true_be._cost_safe_profit_lock
    assert getattr(routes, "_okai_cost_safe_be_risk_v1", False) is True
    assert getattr(true_be, "_okai_all_in_risk_cap_v1", False) is True
