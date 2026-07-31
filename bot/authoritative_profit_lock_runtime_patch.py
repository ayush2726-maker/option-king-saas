"""Authoritative smooth R-based runtime profit lock.

The first protected stop must honour the configured charges + 4% net-profit rule.
After that threshold is genuinely reached, profit is protected gradually with the
existing R-based runner schedule. PAPER stop exits are simulated at the stored stop
with at most one tick of adverse slippage; a slow quote/poll must not turn a valid
profit-lock stop into a large artificial loss. LIVE fills remain broker-controlled.

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
COST_COVER_NET_PROFIT_PERCENT = 4.0
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
AUTHORITY_VERSION = "AUTHORITATIVE_4PCT_SMOOTH_R_TRAIL_V3"
PAPER_FILL_VERSION = "PAPER_STOP_ONE_TICK_FILL_V1"


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
    # Keep one tick above the exact solver price so the PAPER one-tick fill cap
    # still preserves the requested charges + 4% net-profit floor.
    exact_price = max(entry, _f(exact.get("price"), entry))
    cost_cover_price = _round_up_tick(exact_price + TICK_SIZE)

    new_sl = old_sl
    stage = "WAITING_CHARGES_PLUS_4PCT_LOCK"
    triggered = bool(
        peak_r + 1e-9 >= FIRST_LOCK_TRIGGER_R
        and peak + 1e-9 >= cost_cover_price + TICK_SIZE
    )

    if triggered:
        new_sl = max(new_sl, cost_cover_price)
        stage = "CHARGES_PLUS_4PCT_LOCK"

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
        "exact_4pct_solver_price": round(exact_price, 2),
        "breakeven_triggered": triggered,
        "breakeven_rule": "EXACT_CHARGES_PLUS_4PCT_THEN_SMOOTH_R_LOCKS",
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


def _paper_stop_fill_price(conn, trade, observed_price: float, reason: str) -> float:
    """Cap PAPER stop slippage to one tick; LIVE execution is never changed."""
    if runtime._mode(trade) != "paper":
        return round(_f(observed_price), 2)

    reason_text = str(reason or "").upper()
    if "SL HIT" not in reason_text and "PROFIT LOCK TRAIL HIT" not in reason_text:
        return round(_f(observed_price), 2)

    stop_price = _f(runtime._v(trade, "sl_price", 0), 0)
    try:
        row = conn.execute(
            "SELECT sl_price FROM paper_trades WHERE id=? LIMIT 1",
            (_i(runtime._v(trade, "id", 0), 0),),
        ).fetchone()
        if row is not None:
            try:
                stop_price = max(stop_price, _f(row["sl_price"], stop_price))
            except Exception:
                stop_price = max(stop_price, _f(row[0], stop_price))
    except Exception:
        pass

    observed = max(TICK_SIZE, _f(observed_price, TICK_SIZE))
    if stop_price <= 0:
        return _round_down_tick(observed)

    one_tick_below_stop = _round_down_tick(stop_price - TICK_SIZE)
    return round(max(observed, one_tick_below_stop), 2)


def apply_authoritative_profit_lock_runtime_patch() -> None:
    if getattr(runtime, "_okai_authoritative_profit_lock_v3", False):
        apply_trade_visibility_metrics_patch()
        return

    previous_evaluate = runtime._evaluate_exit
    previous_close = runtime._close

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

    def close_with_bounded_paper_stop_fill(
        conn,
        user_id,
        trade,
        price,
        reason,
        order_id=None,
    ):
        adjusted_price = _paper_stop_fill_price(conn, trade, price, reason)
        adjusted_reason = reason
        if runtime._mode(trade) == "paper" and adjusted_price > _f(price) + 1e-9:
            adjusted_reason = (
                f"{reason} | {PAPER_FILL_VERSION}"
                f" | observed={_f(price):.2f}"
                f" | fill={adjusted_price:.2f}"
            )
        return previous_close(
            conn,
            user_id,
            trade,
            adjusted_price,
            adjusted_reason,
            order_id,
        )

    evaluate_with_authoritative_trail._okai_authoritative_profit_lock_v3 = True
    close_with_bounded_paper_stop_fill._okai_paper_stop_fill_v1 = True
    runtime._evaluate_exit = evaluate_with_authoritative_trail
    runtime._close = close_with_bounded_paper_stop_fill
    runtime._okai_authoritative_profit_lock_v3 = True
    runtime._okai_authoritative_profit_lock_v2 = True
    apply_trade_visibility_metrics_patch()
