from bot import authoritative_profit_lock_runtime_patch as trail
from bot import breakeven_4pct_patch as config


def _trade(**overrides):
    data = {
        "id": 101,
        "entry_price": 629.50,
        "sl_price": 594.30,
        "initial_risk": 35.20,
        "peak_price": 629.50,
        "qty": 60,
        "broker_name": "upstox",
        "trading_mode": "paper",
        "underlying": "BANKNIFTY",
        "symbol": "BANKNIFTY 57800 PE 25 AUG 26",
    }
    data.update(overrides)
    return data


def _solver(price):
    def solve(*args, **_kwargs):
        percent = float(args[-1])
        staged_price = {
            0.0: price - 15.0,
            1.0: price - 11.25,
            2.0: price - 7.5,
            4.0: price,
        }[percent]
        return {
            "price": staged_price,
            "target_net_profit": 1510.80,
            "net_pnl_at_price": 1513.59,
            "total_charges_at_price": 140.01,
            "slippage_cost_at_price": 77.27,
            "quantity_basis": 60,
            "instrument_basis": "BANKNIFTY",
            "broker_basis": "upstox",
            "trading_mode_basis": "paper",
        }
    return solve


def test_exact_4pct_lock_triggers_before_one_r(monkeypatch):
    monkeypatch.setattr(trail.live_cost, "calculate_exact_breakeven_price", _solver(658.35))
    monkeypatch.setattr(trail, "FIRST_LOCK_TRIGGER_R", config.FIRST_LOCK_TRIGGER_R)

    result = trail._authoritative_trail(
        _trade(peak_price=662.75),
        662.75,
    )

    assert result["peak_r"] < 1.0
    assert result["breakeven_triggered"] is True
    assert result["four_pct_triggered"] is True
    assert result["sl_price"] == result["four_pct_min_lock_price"]
    assert result["sl_price"] < result["protected_4pct_price"]
    assert result["four_pct_full_lock_armed"] is False
    assert result["stage"] == "LOCK_MIN_1PCT_AFTER_4PCT_NET"
    assert result["sl_price"] < result["peak_price"]
    assert result["trail_schedule"]["four_pct_trigger_r"] == 0.0
    assert result["trail_schedule"]["four_pct_min_peak_room_r"] == 0.50
    assert result["four_pct_min_lock_net_percent"] == 1.0


def test_lock_still_waits_below_exact_4pct_price(monkeypatch):
    monkeypatch.setattr(trail.live_cost, "calculate_exact_breakeven_price", _solver(658.35))
    monkeypatch.setattr(trail, "FIRST_LOCK_TRIGGER_R", config.FIRST_LOCK_TRIGGER_R)

    result = trail._authoritative_trail(
        _trade(peak_price=657.65),
        657.65,
    )

    assert result["breakeven_triggered"] is False
    assert result["four_pct_triggered"] is False
    assert result["sl_price"] == 594.30
    assert result["stage"] == "WAITING_EARLY_COST_SAFE_FLOOR"
    assert result["early_protection_trigger_net_percent"] == 4.0


def test_higher_r_runner_schedule_is_unchanged(monkeypatch):
    monkeypatch.setattr(trail.live_cost, "calculate_exact_breakeven_price", _solver(658.35))
    monkeypatch.setattr(trail, "FIRST_LOCK_TRIGGER_R", config.FIRST_LOCK_TRIGGER_R)

    result = trail._authoritative_trail(
        _trade(peak_price=700.00),
        700.00,
    )

    assert result["peak_r"] >= 2.0
    assert result["sl_price"] >= 664.70
    assert result["stage"] in {
        "LOCK_70PCT_PEAK_PROFIT_AFTER_1_50R",
        "RUNNER_TRAIL_0_65R_AFTER_2_00R",
        "SMOOTH_TRAIL_0_55R_AFTER_2_50R",
        "TIGHT_TRAIL_0_45R_AFTER_4_00R",
    }
