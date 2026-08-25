from datetime import datetime, timedelta, timezone

from bot import dynamic_exit
from bot import expiry_day_risk_mode_patch as expiry
from bot import authoritative_profit_lock_runtime_patch as trail
from bot import auto_portfolio_runtime as runtime


def _scan(score=88, side="CE", bullish=True, mtf_side="CE"):
    return {
        "underlying": "NIFTY",
        "status": "OK",
        "signal_data": {
            "signal": side,
            "candidate_signal": side,
            "score": score,
            "trade_allowed": True,
            "safety_gate_reasons": [],
            "real_mtf_5m": {
                "available": True,
                "side": mtf_side,
            },
        },
        "market_data": {
            "c1_bullish": bullish,
            "c2_bullish": bullish,
        },
    }


def test_expiry_scan_requires_88_momentum_and_real_5m(monkeypatch):
    monkeypatch.setattr(
        expiry,
        "_now_ist",
        lambda: datetime(2026, 8, 25, 10, 15),
    )

    allowed = expiry._apply_scan_guard(_scan())
    assert allowed["signal_data"]["trade_allowed"] is True
    assert allowed["signal_data"]["min_score"] == 88
    assert allowed["signal_data"]["expiry_orb_required"] is False

    low_score = expiry._apply_scan_guard(_scan(score=87))
    assert low_score["signal_data"]["trade_allowed"] is False
    assert "EXPIRY_SCORE_BELOW_88" in low_score["signal_data"]["safety_gate_reasons"]

    no_momentum = expiry._apply_scan_guard(_scan(bullish=False))
    assert no_momentum["signal_data"]["trade_allowed"] is False
    assert "EXPIRY_TWO_CANDLE_MOMENTUM_REQUIRED" in no_momentum["signal_data"]["safety_gate_reasons"]

    wrong_mtf = expiry._apply_scan_guard(_scan(mtf_side="PE"))
    assert wrong_mtf["signal_data"]["trade_allowed"] is False
    assert "EXPIRY_REAL_5M_DIRECTION_REQUIRED" in wrong_mtf["signal_data"]["safety_gate_reasons"]


def test_normal_day_scan_is_unchanged(monkeypatch):
    monkeypatch.setattr(
        expiry,
        "_now_ist",
        lambda: datetime(2026, 8, 24, 10, 15),
    )
    scan = _scan(score=82)
    assert expiry._apply_scan_guard(scan) is scan
    assert scan["signal_data"]["trade_allowed"] is True


def test_expiry_window_and_ist_day_boundaries_are_exact():
    assert expiry._expiry_window_open(datetime(2026, 8, 25, 9, 30)) is True
    assert expiry._expiry_window_open(datetime(2026, 8, 25, 14, 44)) is True
    assert expiry._expiry_window_open(datetime(2026, 8, 25, 14, 45)) is False

    ist = timezone(timedelta(hours=5, minutes=30))
    start, end = expiry._day_bounds_utc(
        datetime(2026, 8, 25, 0, 15, tzinfo=ist)
    )
    assert datetime.fromisoformat(start) == datetime(
        2026, 8, 24, 18, 30, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(end) == datetime(
        2026, 8, 25, 18, 30, tzinfo=timezone.utc
    )


def test_exact_same_day_expiry_resolution():
    now = datetime(2026, 8, 25, 10, 15)
    assert expiry._resolved_expires_today(
        {"expiry_date": "2026-08-25"}, now
    ) is True
    assert expiry._resolved_expires_today(
        {"expiry_date": "2026-09-01"}, now
    ) is False

    # The exact resolved contract is authoritative even for an underlying
    # whose exchange expiry weekday is not inferred by the scan layer.
    assert expiry._entry_is_expiry_session(
        {"underlying": "SENSEX"},
        {"expiry_date": "2026-08-25"},
        now,
    ) is True


def test_runtime_expiry_mode_uses_contract_date_not_tuesday_weekday(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_now_ist",
        lambda: datetime(2026, 8, 25, 10, 15),
    )

    assert runtime._resolved_expires_today({"expiry": "2026-08-25"}) is True
    assert runtime._resolved_expires_today({"expiry": "2026-08-27"}) is False


def test_expiry_quantity_caps_planned_sl_loss_at_ten_percent():
    base = {
        "lots": 21,
        "qty": 1365,
        "slot_budget": 60000.0,
        "target_slot_budget": 60000.0,
    }
    result = expiry._cap_expiry_size(
        base,
        capital_base=120000.0,
        premium=43.45,
        lot_size=65,
        risk_points=10.0,
    )

    assert result["risk_lots"] == 18
    assert result["lots"] == 18
    assert result["qty"] == 1170
    assert result["max_planned_loss_amount"] == 12000.0
    assert result["planned_risk_per_lot"] == 650.0
    assert result["capital_used"] == 50836.5


def test_expiry_atr_has_wider_12_to_20_percent_room():
    floor = dynamic_exit.calculate_option_atr_levels(
        24000,
        100,
        1,
        is_expiry_day=True,
    )
    assert floor["risk_points"] == 12.0
    assert floor["min_premium_risk_percent"] == 12.0
    assert floor["hard_premium_risk_cap_percent"] == 20.0

    capped = dynamic_exit.calculate_option_atr_levels(
        24000,
        100,
        20,
        is_expiry_day=True,
    )
    assert capped["risk_points"] == 20.0
    assert capped["hard_risk_cap_applied"] is True

    normal = dynamic_exit.calculate_option_atr_levels(
        24000,
        100,
        1,
        is_expiry_day=False,
    )
    assert normal["risk_points"] == 10.0
    assert normal["hard_premium_risk_cap_percent"] == 15.0


def test_expiry_cost_floor_waits_for_point_seven_five_r(monkeypatch):
    monkeypatch.setattr(
        trail.runtime,
        "_now_ist",
        lambda: datetime(2026, 8, 25, 10, 15),
    )

    def fake_solver(_broker, _instrument, entry, _qty, _mode, percent):
        prices = {
            0.0: entry,
            1.0: entry + 1.0,
            2.0: entry + 2.0,
            4.0: entry + 4.0,
        }
        return {
            "price": prices[float(percent)],
            "target_net_profit": 0.0,
            "net_pnl_at_price": 0.0,
            "total_charges_at_price": 0.0,
            "slippage_cost_at_price": 0.0,
            "quantity_basis": 65,
            "instrument_basis": "NIFTY",
            "broker_basis": "upstox",
            "trading_mode_basis": "paper",
        }

    monkeypatch.setattr(
        trail.live_cost,
        "calculate_exact_breakeven_price",
        fake_solver,
    )
    trade = {
        "entry_price": 100.0,
        "sl_price": 90.0,
        "initial_risk": 10.0,
        "peak_price": 100.0,
        "qty": 65,
        "broker_name": "upstox",
        "underlying": "NIFTY",
        "trading_mode": "paper",
        "expiry": "2026-08-25",
    }

    early = trail._authoritative_trail(trade, 105.0)
    assert early["breakeven_triggered"] is False
    assert early["four_pct_triggered"] is False
    assert early["sl_price"] == 90.0
    assert early["stage"] == "WAITING_EXPIRY_0_75R_COST_SAFE_FLOOR"

    armed = trail._authoritative_trail(trade, 108.0)
    assert armed["breakeven_triggered"] is True
    assert armed["sl_price"] >= 100.0
    assert armed["expiry_early_floor_min_peak_r"] == 0.75
