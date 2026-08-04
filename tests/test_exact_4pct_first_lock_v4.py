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
    return {
        "price": price,
        "target_net_profit": 1510.80,
        "net_pnl_at_price": 1513.59,
        "total_charges_at_price": 140.01,
        "slippage_cost_at_price": 77.27,
        "quantity_basis": 60,
        "instrument_basis": "BANKNIFTY",
        "broker_basis": "upstox",
        "trading_mode_basis": "paper",
    }


def test_exact_4pct_lock_triggers_before_one_r(monkeypatch):
    monkeypatch.setattr(trail.live_cost, "calculate_exact_breakeven_price", lambda *_a, **_k: _solver(658.35))
    monkeypatch.setattr(trail, "FIRST_LOCK_TRIGGER_R", config.FIRST_LOCK_TRIGGER_R)

    result = trail._authoritative_trail(
        _trade(peak_price=662.75),
        662.75,
    )

    assert result["peak_r"] < 1.0
    assert result["breakeven_triggered"] is True
    assert result["sl_price"] >= 658.40
    assert result["stage"] == "CHARGES_PLUS_4PCT_LOCK"
    assert result["trail_schedule"]["cost_cover_trigger_r"] == 0.0


def test_lock_still_waits_below_exact_4pct_price(monkeypatch):
    monkeypatch.setattr(trail.live_cost, "calculate_exact_breakeven_price", lambda *_a, **_k: _solver(658.35))
    monkeypatch.setattr(trail, "FIRST_LOCK_TRIGGER_R", config.FIRST_LOCK_TRIGGER_R)

    result = trail._authoritative_trail(
        _trade(peak_price=657.65),
        657.65,
    )

    assert result["breakeven_triggered"] is False
    assert result["sl_price"] == 594.30
    assert result["stage"] == "WAITING_CHARGES_PLUS_4PCT_LOCK"


def test_higher_r_runner_schedule_is_unchanged(monkeypatch):
    monkeypatch.setattr(trail.live_cost, "calculate_exact_breakeven_price", lambda *_a, **_k: _solver(658.35))
    monkeypatch.setattr(trail, "FIRST_LOCK_TRIGGER_R", config.FIRST_LOCK_TRIGGER_R)

    result = trail._authoritative_trail(
        _trade(peak_price=700.00),
        700.00,
    )

    assert result["peak_r"] >= 2.0
    assert result["sl_price"] >= 664.70
    assert result["stage"] in {
        "LOCK_1_00R_AFTER_2_00R",
        "SMOOTH_TRAIL_1_00R_AFTER_2_50R",
        "TIGHT_TRAIL_0_80R_AFTER_4_00R",
    }
