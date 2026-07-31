"""Authoritative smooth R-based runtime profit lock.

The active PAPER/LIVE monitor must not leave the original ATR stop unchanged after
an option has moved materially in profit.  The first lock now covers the actual
round-trip execution costs instead of waiting for an additional fixed 4% premium
move.  Profit is then protected gradually so normal option noise still has room:

* +1.00R: lock exact round-trip costs (net break-even);
* +1.50R: lock at least +0.50R;
* +2.00R: lock at least +1.00R;
* +2.50R: trail 1.00R behind the peak and lock at least +1.25R;
* +4.00R: trail 0.80R behind the peak and lock at least +2.50R.

The stop can only move upward. Entries, quantities, structural exits, fixed-target
behaviour and EOD rules are not changed.
"""

from __future__ import annotations

import math
from typing import Any, Dict

from bot import auto_portfolio_runtime as runtime
from bot import live_net_pnl_breakeven_patch as live_cost
from bot.trade_visibility_metrics_patch import apply_trade_visibility_metrics_patch


TICK_SIZE = 0.05
COST_COVER_NET_PROFIT_PERCENT = 0.0
FIRST_LOCK_TRIGGER_R = 1.00
LOCK_1_TRIGGER_R = 1.50
LOCK_1_R = 0.50
LOCK_2_TRIGGER_R = 2.00
LOCK_2_R = 1.00
SMOOTH_TRAIL_TRIGGER_R = 2.50
SMOOTH_TRAIL_MIN_LOCK_R = 1.25
SMOOTH_TRAIL_DISTANCE_R = 1.00
TIGHT_TRAIL_TRIGGER_R = 4.00
TIGHT_TRAIL_MIN_LOCK_R = 2.50
TIGHT_TRAIL_DISTANCE_R = 0.80
AUTHORITY_VERSION = "AUTHORITATIVE_SMOOTH_R_TRAIL_V2"


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


def _round_up_tick(value: float) -> float:
    ticks = math.ceil(max(TICK_SIZE, _f(value, TICK_SIZE)) / TICK_SIZE - 1e-12)
    return round(ticks * TICK_SIZE, 2)


def _round_down_tick(value: float) -> float:
    ticks = math.floor(max(TICK_SIZE, _f(value, TICK_SIZE)) / TICK_SIZE + 1e-12)
    return round(max(TICK_SIZE, ticks * TICK_SIZE), 2)


def _authoritative_trail(trade, current_price: float) -> Dict[str, Any]:
    entry = max(
        TICK_SIZE,
        _f(runtime._v(trade, "entry_price", TICK_SIZE), TICK_SIZE),
    )
    old_sl = max(
        TICK_SIZE,
        _f(
            runtime._v(trade, "sl_price", entry - TICK_SIZE),
            entry - TICK_SIZE,
        ),
    )
    risk = max(
        TICK_SIZE,
        _f(
            runtime._v(trade, "initial_risk", entry - old_sl),
            entry - old_sl,
        ),
    )
    current = max(TICK_SIZE, _f(current_price, TICK_SIZE))
    saved_peak = max(
        entry,
        _f(runtime._v(trade, "peak_price", entry), entry),
    )
    peak = max(entry, saved_peak, current)
    peak_r = max(0.0, (peak - entry) / risk)

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
        COST_COVER_NET_PROFIT_PERCENT,
    )
    cost_cover_price = max(entry, _f(exact.get("price"), entry))

    new_sl = old_sl
    stage = "WAITING_COST_COVER_LOCK_AT_1R"
    triggered = bool(
        peak_r + 1e-9 >= FIRST_LOCK_TRIGGER_R
        and peak + 1e-9 >= cost_cover_price + TICK_SIZE
    )

    if triggered:
        new_sl = max(new_sl, cost_cover_price)
        stage = "COST_COVER_LOCK_AT_1R"

        if peak_r + 1e-9 >= LOCK_1_TRIGGER_R:
            new_sl = max(new_sl, entry + LOCK_1_R * risk)
            stage = "LOCK_0_50R_AFTER_1_50R"

        if peak_r + 1e-9 >= LOCK_2_TRIGGER_R:
            new_sl = max(new_sl, entry + LOCK_2_R * risk)
            stage = "LOCK_1_00R_AFTER_2_00R"

        if peak_r + 1e-9 >= SMOOTH_TRAIL_TRIGGER_R:
            new_sl = max(
                new_sl,
                entry + SMOOTH_TRAIL_MIN_LOCK_R * risk,
                peak - SMOOTH_TRAIL_DISTANCE_R * risk,
            )
            stage = "SMOOTH_TRAIL_1_00R_AFTER_2_50R"

        if peak_r + 1e-9 >= TIGHT_TRAIL_TRIGGER_R:
            new_sl = max(
                new_sl,
                entry + TIGHT_TRAIL_MIN_LOCK_R * risk,
                peak - TIGHT_TRAIL_DISTANCE_R * risk,
            )
            stage = "TIGHT_TRAIL_0_80R_AFTER_4_00R"

    # A protected stop must stay below the observed peak by at least one tick.
    # Round the desired stop upward, but cap it to a valid tradable tick below peak.
    peak_room = _round_down_tick(peak - TICK_SIZE)
    desired = _round_up_tick(new_sl)
    candidate = min(desired, peak_room)
    candidate = max(old_sl, candidate)
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
        "cost_safe_breakeven_price": round(cost_cover_price, 2),
        "breakeven_triggered": triggered,
        "breakeven_rule": "EXACT_COST_COVER_AT_1R_THEN_SMOOTH_R_LOCKS",
        "breakeven_net_profit_percent": COST_COVER_NET_PROFIT_PERCENT,
        "breakeven_target_net_profit": exact.get("target_net_profit"),
        "breakeven_net_pnl_at_stop": exact.get("net_pnl_at_price"),
        "breakeven_total_charges": exact.get("total_charges_at_price"),
        "breakeven_slippage_cost": exact.get("slippage_cost_at_price"),
        "breakeven_quantity_basis": exact.get("quantity_basis"),
        "breakeven_instrument_basis": exact.get("instrument_basis"),
        "breakeven_broker_basis": exact.get("broker_basis"),
        "breakeven_trading_mode_basis": exact.get("trading_mode_basis"),
        "profit_lock_authority": AUTHORITY_VERSION,
        "trail_schedule": {
            "cost_cover_trigger_r": FIRST_LOCK_TRIGGER_R,
            "lock_0_5r_trigger_r": LOCK_1_TRIGGER_R,
            "lock_1r_trigger_r": LOCK_2_TRIGGER_R,
            "smooth_trail_trigger_r": SMOOTH_TRAIL_TRIGGER_R,
            "smooth_trail_distance_r": SMOOTH_TRAIL_DISTANCE_R,
            "tight_trail_trigger_r": TIGHT_TRAIL_TRIGGER_R,
            "tight_trail_distance_r": TIGHT_TRAIL_DISTANCE_R,
        },
    }


def apply_authoritative_profit_lock_runtime_patch() -> None:
    if getattr(runtime, "_okai_authoritative_profit_lock_v2", False):
        apply_trade_visibility_metrics_patch()
        return

    previous_evaluate = runtime._evaluate_exit

    def evaluate_with_authoritative_trail(trade, ltp, market_data, candle_id):
        result = dict(
            previous_evaluate(trade, ltp, market_data, candle_id) or {}
        )
        trail = _authoritative_trail(trade, ltp)
        result["trail"] = trail
        result["risk"] = trail["initial_risk"]

        previous_updates = _i(runtime._v(trade, "trail_updates", 0), 0)
        result["updates"] = previous_updates + (1 if trail["updated"] else 0)

        # Replace only a prior premium-SL/profit-lock decision. Structural and EOD
        # exits from the wrapped evaluator remain authoritative.
        reason = result.get("reason")
        reason_text = str(reason or "").upper()
        if (
            "PURE ATR SL HIT" in reason_text
            or "PROFIT LOCK TRAIL HIT" in reason_text
        ):
            reason = None

        if reason is None and _f(ltp) <= _f(trail.get("sl_price")):
            if _f(trail.get("sl_price")) >= _f(
                runtime._v(trade, "entry_price", 0)
            ):
                reason = (
                    "PROFIT LOCK TRAIL HIT"
                    f" | {trail['stage']}"
                    f" | locked={trail['locked_r']}R"
                )
            else:
                reason = "PURE ATR SL HIT"

        result["reason"] = reason
        result["profit_lock_authority"] = AUTHORITY_VERSION
        return result

    evaluate_with_authoritative_trail._okai_authoritative_profit_lock_v2 = True
    runtime._evaluate_exit = evaluate_with_authoritative_trail
    runtime._okai_authoritative_profit_lock_v2 = True
    apply_trade_visibility_metrics_patch()
