from bot import authoritative_profit_lock_runtime_patch as smooth
from bot import live_net_pnl_breakeven_patch as live_cost


def _trade(
    *,
    entry=100.0,
    sl=90.0,
    risk=10.0,
    peak=100.0,
    qty=30,
    broker="angelone",
    underlying="BANKNIFTY",
    mode="paper",
):
    return {
        "entry_price": entry,
        "sl_price": sl,
        "initial_risk": risk,
        "peak_price": peak,
        "qty": qty,
        "broker_name": broker,
        "underlying": underlying,
        "trading_mode": mode,
        "symbol": f"{underlying} TEST CE",
        "side": "CE",
    }


def test_high_premium_example_locks_profit_near_peak_minus_one_r():
    trade = _trade(
        entry=871.0,
        sl=861.08,
        risk=9.92,
        peak=871.0,
        qty=30,
        broker="upstox",
    )
    trail = smooth._authoritative_trail(trade, 899.55)

    assert trail["peak_r"] == 2.88
    assert trail["stage"] == "SMOOTH_TRAIL_0_55R_AFTER_2_50R"
    assert 894.05 <= trail["sl_price"] <= 894.15
    assert trail["sl_price"] > trade["entry_price"]
    assert trail["updated"] is True


def test_one_r_ratchet_covers_exact_paper_costs():
    trade = _trade(entry=100.0, sl=90.0, risk=10.0, peak=100.0)
    trail = smooth._authoritative_trail(trade, 110.0)

    assert trail["stage"] == "LOCK_0_45R_AFTER_1_00R"
    assert trail["sl_price"] >= 104.5
    assert trail["breakeven_net_profit_percent"] == 4.0

    costs = live_cost.calculate_execution_costs(
        "angelone",
        "BANKNIFTY",
        100.0,
        trail["cost_safe_breakeven_price"],
        30,
        True,
    )
    assert costs["net_pnl"] >= 0


def test_gradual_lock_stages_ratchet_without_choking_runner():
    trade = _trade(entry=100.0, sl=90.0, risk=10.0, peak=100.0)

    before = smooth._authoritative_trail(trade, 109.95)
    at_one_and_half = smooth._authoritative_trail(trade, 115.0)
    at_two = smooth._authoritative_trail(trade, 120.0)

    assert before["stage"] == "LOCK_0_25R_AFTER_0_75R"
    assert before["sl_price"] >= 102.5
    assert at_one_and_half["stage"] == "LOCK_0_80R_AFTER_1_50R"
    assert at_one_and_half["sl_price"] >= 108.0
    assert at_two["stage"] == "RUNNER_TRAIL_0_65R_AFTER_2_00R"
    assert at_two["sl_price"] >= 113.5


def test_extended_winner_uses_tighter_point_eight_r_trail():
    trade = _trade(entry=100.0, sl=90.0, risk=10.0, peak=100.0)
    trail = smooth._authoritative_trail(trade, 140.0)

    assert trail["stage"] == "TIGHT_TRAIL_0_45R_AFTER_4_00R"
    assert trail["sl_price"] == 135.5
    assert trail["locked_r"] == 3.55


def test_stop_never_moves_backward():
    trade = _trade(
        entry=100.0,
        sl=112.0,
        risk=10.0,
        peak=115.0,
    )
    trail = smooth._authoritative_trail(trade, 114.0)

    assert trail["sl_price"] >= 112.0
    assert trail["updated"] is False
