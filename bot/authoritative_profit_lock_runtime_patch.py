"""Authoritative runtime profit-lock guard.

The active PAPER/LIVE monitor must never leave an initial ATR stop unchanged after
the configured profit-lock thresholds have been crossed. This guard runs after all
legacy exit wrappers and recalculates the final stop from the actual trade context:

* first lock: exact round-trip costs + 4% net profit, only after +0.75R;
* +1.35R: lock at least +0.50R;
* +2.20R: lock at least +1.20R and trail 1.00R behind peak;
* +3.20R: lock at least +2.00R and trail 1.20R behind peak.

It does not change entries, quantities, fixed targets, structural exits or EOD rules.
"""

from __future__ import annotations

from typing import Any, Dict

from bot import auto_portfolio_runtime as runtime
from bot import live_net_pnl_breakeven_patch as live_cost


TICK_SIZE = 0.05
NET_PROFIT_LOCK_PERCENT = 4.0
FIRST_LOCK_TRIGGER_R = 0.75
LOCK_1_TRIGGER_R = 1.35
LOCK_1_R = 0.50
LOCK_2_TRIGGER_R = 2.20
LOCK_2_R = 1.20
LOCK_2_TRAIL_R = 1.00
LOCK_3_TRIGGER_R = 3.20
LOCK_3_R = 2.00
LOCK_3_TRAIL_R = 1.20


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except (TypeError, ValueError):
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _authoritative_trail(trade, current_price: float) -> Dict[str, Any]:
    entry = max(TICK_SIZE, _f(runtime._v(trade, "entry_price", TICK_SIZE), TICK_SIZE))
    old_sl = max(
        TICK_SIZE,
        _f(runtime._v(trade, "sl_price", entry - TICK_SIZE), entry - TICK_SIZE),
    )
    risk = max(
        TICK_SIZE,
        _f(runtime._v(trade, "initial_risk", entry - old_sl), entry - old_sl),
    )
    current = max(TICK_SIZE, _f(current_price, TICK_SIZE))
    saved_peak = max(entry, _f(runtime._v(trade, "peak_price", entry), entry))
    peak = max(entry, saved_peak, current)
    peak_r = (peak - entry) / risk

    broker_name = str(
        runtime._v(trade, "broker_name", "angelone") or "angelone"
    ).lower()
    instrument = runtime._underlying(trade)
    quantity = max(1, _i(runtime._v(trade, "qty", 1), 1))
    mode = runtime._mode(trade)

    exact = live_cost.calculate_exact_breakeven_price(
        broker_name,
        instrument,
        round(entry, 4),
        quantity,
        mode,
        NET_PROFIT_LOCK_PERCENT,
    )
    cost_safe_price = max(entry, _f(exact.get("price"), entry))

    new_sl = old_sl
    stage = "WAITING_FIRST_PROFIT_LOCK_0_75R"
    triggered = bool(
        peak_r + 1e-9 >= FIRST_LOCK_TRIGGER_R
        and peak + 1e-9 >= cost_safe_price + TICK_SIZE
    )

    if triggered:
        new_sl = max(new_sl, cost_safe_price)
        stage = "COSTS_PLUS_4PCT_LOCK_AFTER_0_75R"

        if peak_r + 1e-9 >= LOCK_1_TRIGGER_R:
            new_sl = max(new_sl, entry + LOCK_1_R * risk)
            stage = "LOCK_0_50R_AFTER_1_35R"

        if peak_r + 1e-9 >= LOCK_2_TRIGGER_R:
            new_sl = max(
                new_sl,
                entry + LOCK_2_R * risk,
                peak - LOCK_2_TRAIL_R * risk,
            )
            stage = "TRAIL_1_00R_AFTER_2_20R"

        if peak_r + 1e-9 >= LOCK_3_TRIGGER_R:
            new_sl = max(
                new_sl,
                entry + LOCK_3_R * risk,
                peak - LOCK_3_TRAIL_R * risk,
            )
            stage = "TRAIL_1_20R_AFTER_3_20R"

    candidate = min(new_sl, max(TICK_SIZE, peak - TICK_SIZE))
    updated = candidate > old_sl + 1e-9

    return {
        "sl_price": round(candidate, 2),
        "old_sl_price": round(old_sl, 2),
        "updated": bool(updated),
        "peak_price": round(peak, 2),
        "peak_r": round(peak_r, 2),
        "locked_r": round((candidate - entry) / risk, 2),
        "stage": stage,
        "initial_risk": round(risk, 2),
        "cost_safe_breakeven_price": round(cost_safe_price, 2),
        "breakeven_triggered": triggered,
        "breakeven_rule": "ENTRY_PLUS_EXACT_COSTS_PLUS_4PCT_NET_AND_0_75R",
        "breakeven_net_profit_percent": NET_PROFIT_LOCK_PERCENT,
        "breakeven_target_net_profit": exact.get("target_net_profit"),
        "breakeven_net_pnl_at_stop": exact.get("net_pnl_at_price"),
        "breakeven_total_charges": exact.get("total_charges_at_price"),
        "breakeven_slippage_cost": exact.get("slippage_cost_at_price"),
        "breakeven_quantity_basis": exact.get("quantity_basis"),
        "breakeven_instrument_basis": exact.get("instrument_basis"),
        "breakeven_broker_basis": exact.get("broker_basis"),
        "breakeven_trading_mode_basis": exact.get("trading_mode_basis"),
        "profit_lock_authority": "AUTHORITATIVE_RUNTIME_GUARD_V1",
    }


def apply_authoritative_profit_lock_runtime_patch() -> None:
    if getattr(runtime, "_okai_authoritative_profit_lock_v1", False):
        return

    previous_evaluate = runtime._evaluate_exit

    def evaluate_with_authoritative_trail(trade, ltp, market_data, candle_id):
        result = dict(previous_evaluate(trade, ltp, market_data, candle_id) or {})
        trail = _authoritative_trail(trade, ltp)
        result["trail"] = trail
        result["risk"] = trail["initial_risk"]

        previous_updates = _i(runtime._v(trade, "trail_updates", 0), 0)
        result["updates"] = previous_updates + (1 if trail["updated"] else 0)

        reason = result.get("reason")
        reason_text = str(reason or "").upper()
        if (
            "PURE ATR SL HIT" in reason_text
            or "PROFIT LOCK TRAIL HIT" in reason_text
        ):
            reason = None

        if reason is None and _f(ltp) <= _f(trail.get("sl_price")):
            if _f(trail.get("sl_price")) >= _f(runtime._v(trade, "entry_price", 0)):
                reason = (
                    "PROFIT LOCK TRAIL HIT"
                    f" | {trail['stage']}"
                    f" | locked={trail['locked_r']}R"
                )
            else:
                reason = "PURE ATR SL HIT"

        result["reason"] = reason
        result["profit_lock_authority"] = "AUTHORITATIVE_RUNTIME_GUARD_V1"
        return result

    evaluate_with_authoritative_trail._okai_authoritative_profit_lock_v1 = True
    runtime._evaluate_exit = evaluate_with_authoritative_trail
    runtime._okai_authoritative_profit_lock_v1 = True
