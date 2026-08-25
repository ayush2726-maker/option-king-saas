from bot import authoritative_profit_lock_runtime_patch as patch


class FakeConn:
    def __init__(self, sl_price):
        self.sl_price = sl_price

    def execute(self, _sql, _params):
        return self

    def fetchone(self):
        return {"sl_price": self.sl_price}


def _trade(**overrides):
    data = {
        "id": 7,
        "entry_price": 100.0,
        "sl_price": 95.0,
        "initial_risk": 5.0,
        "peak_price": 100.0,
        "qty": 50,
        "broker_name": "upstox",
        "trading_mode": "paper",
        "underlying": "NIFTY",
        "symbol": "NIFTY TEST CE",
    }
    data.update(overrides)
    return data


def test_trail_waits_for_exact_charges_plus_4pct(monkeypatch):
    monkeypatch.setattr(
        patch.live_cost,
        "calculate_exact_breakeven_price",
        lambda *_args, **_kwargs: {
            "price": 110.0,
            "target_net_profit": 400.0,
            "net_pnl_at_price": 400.0,
        },
    )

    waiting = patch._authoritative_trail(
        _trade(peak_price=109.0),
        109.0,
    )
    assert waiting["breakeven_triggered"] is False
    assert waiting["sl_price"] == 95.0
    assert waiting["breakeven_net_profit_percent"] == 4.0

    locked = patch._authoritative_trail(
        _trade(peak_price=111.0),
        111.0,
    )
    assert locked["breakeven_triggered"] is True
    assert locked["sl_price"] >= 110.05
    assert "4PCT" in locked["breakeven_rule"]


def test_paper_stop_fill_is_capped_to_one_tick_below_saved_stop():
    trade = _trade(sl_price=95.0)
    fill = patch._paper_stop_fill_price(
        FakeConn(110.05),
        trade,
        102.0,
        "PROFIT LOCK TRAIL HIT | CHARGES_PLUS_4PCT_LOCK",
    )
    assert fill == 110.0


def test_live_and_non_stop_exits_are_not_repriced():
    live_trade = _trade(trading_mode="live")
    assert patch._paper_stop_fill_price(
        FakeConn(110.05),
        live_trade,
        102.0,
        "PROFIT LOCK TRAIL HIT",
    ) == 102.0

    paper_trade = _trade()
    assert patch._paper_stop_fill_price(
        FakeConn(110.05),
        paper_trade,
        102.0,
        "MANUAL EXIT BY USER",
    ) == 102.0

def _staged_solver(*args, **_kwargs):
    percent = float(args[-1])
    prices = {0.0: 101.0, 1.0: 102.0, 2.0: 103.0, 4.0: 105.0}
    price = prices[percent]
    return {
        "price": price,
        "target_net_profit": percent * 10.0,
        "net_pnl_at_price": percent * 10.0,
        "total_charges_at_price": 50.0,
    }


def test_two_percent_move_does_not_arm_profit_trail(monkeypatch):
    monkeypatch.setattr(
        patch.live_cost,
        "calculate_exact_breakeven_price",
        _staged_solver,
    )
    result = patch._authoritative_trail(
        _trade(
            entry_price=100.0,
            sl_price=90.0,
            initial_risk=10.0,
            peak_price=103.10,
        ),
        103.10,
    )
    assert result["breakeven_triggered"] is False
    assert result["four_pct_triggered"] is False
    assert result["sl_price"] == 90.0
    assert result["stage"] == "WAITING_EARLY_COST_SAFE_FLOOR"
    assert result["early_protection_trigger_net_percent"] == 4.0


def test_four_percent_move_guarantees_one_percent_net_floor(monkeypatch):
    monkeypatch.setattr(
        patch.live_cost,
        "calculate_exact_breakeven_price",
        _staged_solver,
    )
    monkeypatch.setattr(patch, "FIRST_LOCK_TRIGGER_R", 0.0)
    result = patch._authoritative_trail(
        _trade(
            entry_price=100.0,
            sl_price=90.0,
            initial_risk=10.0,
            peak_price=105.10,
        ),
        105.10,
    )
    assert result["four_pct_triggered"] is True
    assert result["sl_price"] >= result["four_pct_min_lock_price"]
    assert result["sl_price"] < result["protected_4pct_price"]
    assert result["sl_price"] < result["peak_price"]
    assert result["four_pct_full_lock_armed"] is False
    assert result["four_pct_preferred_room_preserved"] is False
    assert result["stage"] == "LOCK_MIN_1PCT_AFTER_4PCT_NET"


def test_latched_profit_stop_outranks_structural_or_danger_exit():
    trade = _trade(entry_price=100.0, sl_price=90.0, initial_risk=10.0)
    trail = {
        "sl_price": 103.05,
        "stage": "LOCK_4PCT_AFTER_4PCT_NET",
        "locked_r": 0.31,
    }
    reason = patch._protected_profit_stop_reason(trade, 98.0, trail)
    assert reason.startswith("PROFIT LOCK TRAIL HIT")
    assert "LOCK_4PCT_AFTER_4PCT_NET" in reason


def test_friday_style_winner_cannot_close_as_unprotected_loss(monkeypatch):
    def friday_solver(*args, **_kwargs):
        percent = float(args[-1])
        prices = {0.0: 457.0, 1.0: 461.0, 2.0: 466.0, 4.0: 475.0}
        return {
            "price": prices[percent],
            "target_net_profit": percent * 100.0,
            "net_pnl_at_price": percent * 100.0,
        }

    monkeypatch.setattr(
        patch.live_cost,
        "calculate_exact_breakeven_price",
        friday_solver,
    )
    monkeypatch.setattr(patch, "FIRST_LOCK_TRIGGER_R", 0.0)
    trade = _trade(
        entry_price=453.60,
        sl_price=408.24,
        initial_risk=45.36,
        peak_price=487.40,
        qty=100,
        underlying="SENSEX",
    )
    trail = patch._authoritative_trail(trade, 450.40)
    assert trail["peak_price"] == 487.40
    assert trail["sl_price"] >= trail["cost_floor_price"]
    assert trail["sl_price"] >= trail["four_pct_min_lock_price"]
    assert trail["sl_price"] < trail["protected_4pct_price"]
    assert trail["peak_price"] - trail["sl_price"] >= 0.50 * trail["initial_risk"]
    reason = patch._protected_profit_stop_reason(trade, 450.40, trail)
    assert reason.startswith("PROFIT LOCK TRAIL HIT")


def test_sensex_189r_winner_ratchets_from_legacy_half_r_stop(monkeypatch):
    monkeypatch.setattr(
        patch.live_cost,
        "calculate_exact_breakeven_price",
        lambda *_args, **_kwargs: {
            "price": 293.0,
            "target_net_profit": 400.0,
            "net_pnl_at_price": 400.0,
        },
    )
    result = patch._authoritative_trail(
        _trade(
            entry_price=286.95,
            sl_price=302.10,
            initial_risk=28.70,
            peak_price=341.05,
            qty=200,
            underlying="SENSEX",
        ),
        341.05,
    )

    assert result["peak_r"] == 1.89
    assert result["stage"] == "LOCK_70PCT_PEAK_PROFIT_AFTER_1_50R"
    assert result["sl_price"] == 324.85
    assert result["peak_profit_retained_percent"] >= 70.0
    assert result["updated"] is True


def test_banknifty_expiry_winner_retains_seventy_percent_of_peak(monkeypatch):
    def expiry_solver(*args, **_kwargs):
        percent = float(args[-1])
        prices = {0.0: 72.70, 1.0: 73.45, 2.0: 74.20, 4.0: 75.70}
        return {
            "price": prices[percent],
            "target_net_profit": percent * 100.0,
            "net_pnl_at_price": percent * 100.0,
        }

    monkeypatch.setattr(
        patch.live_cost,
        "calculate_exact_breakeven_price",
        expiry_solver,
    )
    result = patch._authoritative_trail(
        _trade(
            entry_price=72.30,
            sl_price=57.85,
            initial_risk=14.45,
            peak_price=95.60,
            qty=690,
            underlying="BANKNIFTY",
            symbol="BANKNIFTY 57400 PE 25 AUG 26",
            expiry="2026-08-25",
        ),
        95.60,
    )

    assert result["peak_r"] == 1.61
    assert result["sl_price"] == 88.65
    assert result["stage"] == "LOCK_70PCT_PEAK_PROFIT_AFTER_1_50R"
    assert result["peak_profit_retained_percent"] >= 70.0
    assert result["peak_price"] - result["sl_price"] <= 7.0


def test_apply_reasserts_authority_after_later_exit_replacement(monkeypatch):
    def replacement_exit(_trade, _ltp, _market_data, _candle_id):
        return {"reason": None, "trail": {"sl_price": 95.0}}

    def current_close(*_args, **_kwargs):
        return None

    monkeypatch.setattr(patch.runtime, "_evaluate_exit", replacement_exit)
    monkeypatch.setattr(patch.runtime, "_close", current_close)
    monkeypatch.setattr(
        patch.runtime, "_okai_authoritative_profit_lock_v3", True, raising=False
    )
    monkeypatch.setattr(patch, "apply_trade_visibility_metrics_patch", lambda: None)
    monkeypatch.setattr(
        patch.live_cost,
        "calculate_exact_breakeven_price",
        lambda *_args, **_kwargs: {
            "price": 101.0,
            "target_net_profit": 0.0,
            "net_pnl_at_price": 0.0,
        },
    )

    patch.apply_authoritative_profit_lock_runtime_patch()

    assert patch.runtime._evaluate_exit is not replacement_exit
    assert getattr(
        patch.runtime._evaluate_exit, "_okai_authoritative_profit_lock_v3", False
    )
    result = patch.runtime._evaluate_exit(
        _trade(entry_price=100.0, sl_price=95.0, initial_risk=5.0),
        110.0,
        None,
        None,
    )
    assert result["trail"]["sl_price"] > 100.0
