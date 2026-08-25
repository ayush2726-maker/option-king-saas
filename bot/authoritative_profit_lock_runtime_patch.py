"""Authoritative smooth R-based runtime profit lock.

The first protected stop now arms only at charges + 4% net profit. Before that
threshold the initial ATR stop is left untouched. At charges + 4%, the protected
stop is capped so the observed peak retains at least 0.50R breathing room, then
the existing R-based runner stages take over. This keeps room for a winner while
preventing a meaningful observed profit from returning to an unprotected loss.

From 1.50R onward at least 70% of the observed peak premium profit is retained.
This caps giveback at 30% without reintroducing an early 2% trigger.

A latched profit stop is authoritative over danger, structural and CAS exits. PAPER
stop exits are simulated at the saved stop with at most one tick of adverse
slippage; LIVE fills remain broker-controlled. The stop can only move upward.
Entries, quantities, initial ATR SL, fixed-target and EOD rules are not changed.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Dict

from bot import auto_portfolio_runtime as runtime
from bot import live_net_pnl_breakeven_patch as live_cost
from bot import auto_hero_zero_runtime as auto_hero
from bot.trade_visibility_metrics_patch import apply_trade_visibility_metrics_patch


TICK_SIZE = 0.05
COST_COVER_NET_PROFIT_PERCENT = 4.0
EARLY_PROTECTION_TRIGGER_NET_PERCENT = 4.0
EARLY_PROTECTION_LOCK_NET_PERCENT = 0.0
FOUR_PCT_MIN_LOCK_NET_PERCENT = 1.0
FOUR_PCT_LOCK_NET_PERCENT = 4.0
FOUR_PCT_MIN_PEAK_ROOM_R = 0.50
EXPIRY_EARLY_FLOOR_MIN_PEAK_R = 0.75
FIRST_LOCK_TRIGGER_R = 1.00
LOCK_0_TRIGGER_R = 0.75
LOCK_0_R = 0.25
LOCK_1_TRIGGER_R = 1.00
LOCK_1_R = 0.45
LOCK_2_TRIGGER_R = 1.50
LOCK_2_R = 0.80
PEAK_PROFIT_RETAIN_TRIGGER_R = 1.50
PEAK_PROFIT_RETAIN_PERCENT = 70.0
RUNNER_TRIGGER_R = 2.00
RUNNER_MIN_LOCK_R = 1.20
RUNNER_DISTANCE_R = 0.65
SMOOTH_TRAIL_TRIGGER_R = 2.50
SMOOTH_TRAIL_MIN_LOCK_R = 1.75
SMOOTH_TRAIL_DISTANCE_R = 0.55
TIGHT_TRAIL_TRIGGER_R = 4.00
TIGHT_TRAIL_MIN_LOCK_R = 3.00
TIGHT_TRAIL_DISTANCE_R = 0.45
AUTHORITY_VERSION = "AUTHORITATIVE_4PCT_PEAK70_PROFIT_RATCHET_V9"
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


def _is_same_day_expiry_trade(trade: Any) -> bool:
    value = runtime._v(trade, "expiry")
    if isinstance(value, datetime):
        expiry = value.date()
    elif isinstance(value, date):
        expiry = value
    else:
        expiry = None
        text = str(value or "").strip().upper()
        for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%m-%Y"):
            try:
                expiry = datetime.strptime(text, fmt).date()
                break
            except Exception:
                pass
    return bool(expiry and expiry == runtime._now_ist().date())


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
    expiry_day_trade = _is_same_day_expiry_trade(trade)

    broker_name = str(
        runtime._v(trade, "broker_name", "angelone") or "angelone"
    ).lower()
    instrument = runtime._underlying(trade)
    quantity = max(1, _i(runtime._v(trade, "qty", 1), 1))
    mode = runtime._mode(trade)

    exact_floor = live_cost.calculate_exact_breakeven_price(
        broker_name,
        instrument,
        round(entry, 4),
        quantity,
        mode,
        EARLY_PROTECTION_LOCK_NET_PERCENT,
    )
    exact_early = live_cost.calculate_exact_breakeven_price(
        broker_name,
        instrument,
        round(entry, 4),
        quantity,
        mode,
        EARLY_PROTECTION_TRIGGER_NET_PERCENT,
    )
    exact_four_pct_floor = live_cost.calculate_exact_breakeven_price(
        broker_name,
        instrument,
        round(entry, 4),
        quantity,
        mode,
        FOUR_PCT_MIN_LOCK_NET_PERCENT,
    )
    exact = live_cost.calculate_exact_breakeven_price(
        broker_name,
        instrument,
        round(entry, 4),
        quantity,
        mode,
        COST_COVER_NET_PROFIT_PERCENT,
    )

    # Keep protected stops one tick above their exact solver price. The PAPER
    # one-tick fill cap can then still preserve the requested net floor.
    exact_floor_price = max(entry, _f(exact_floor.get("price"), entry))
    exact_early_price = max(entry, _f(exact_early.get("price"), entry))
    exact_four_pct_floor_price = max(
        entry,
        _f(exact_four_pct_floor.get("price"), entry),
    )
    exact_price = max(entry, _f(exact.get("price"), entry))
    cost_floor_price = _round_up_tick(exact_floor_price + TICK_SIZE)
    early_trigger_price = _round_up_tick(exact_early_price + TICK_SIZE)
    protected_4pct_price = early_trigger_price
    four_pct_min_lock_price = _round_up_tick(
        exact_four_pct_floor_price + TICK_SIZE
    )
    cost_cover_price = _round_up_tick(exact_price + TICK_SIZE)
    four_pct_room_cap_price = _round_down_tick(
        peak - FOUR_PCT_MIN_PEAK_ROOM_R * risk
    )
    preferred_four_pct_lock_price = min(
        protected_4pct_price,
        four_pct_room_cap_price,
    )
    # At charges + 4% net, never let the room cap push the protected stop all
    # the way back to a zero-net cost floor.  Keep the requested 0.50R room when
    # it is available; otherwise a modest 1% net floor wins.  This leaves much
    # more breathing room than a full +4% lock while preventing the
    # observed winner from closing at only a few rupees after costs.
    four_pct_breathing_lock_price = min(
        protected_4pct_price,
        max(four_pct_min_lock_price, preferred_four_pct_lock_price),
    )

    new_sl = old_sl
    stage = (
        "WAITING_EXPIRY_0_75R_COST_SAFE_FLOOR"
        if expiry_day_trade
        else "WAITING_EARLY_COST_SAFE_FLOOR"
    )

    early_triggered = bool(
        peak + 1e-9 >= early_trigger_price
        and (
            not expiry_day_trade
            or peak_r + 1e-9 >= EXPIRY_EARLY_FLOOR_MIN_PEAK_R
        )
    )
    four_pct_triggered = bool(
        peak_r + 1e-9 >= FIRST_LOCK_TRIGGER_R
        and peak + 1e-9 >= cost_cover_price
        and (not expiry_day_trade or early_triggered)
    )

    if early_triggered:
        new_sl = max(new_sl, cost_floor_price)
        stage = "COST_SAFE_FLOOR_AFTER_4PCT_NET"

    if four_pct_triggered:
        new_sl = max(new_sl, four_pct_breathing_lock_price)
        stage = (
            "LOCK_4PCT_AFTER_4PCT_NET"
            if four_pct_breathing_lock_price + 1e-9 >= protected_4pct_price
            else (
                "LOCK_MIN_1PCT_AFTER_4PCT_NET"
                if preferred_four_pct_lock_price + 1e-9 < four_pct_min_lock_price
                else "FOUR_PCT_LOCK_WITH_0_50R_ROOM"
            )
        )

    # Progressive profit ratchet: protect more as the move proves itself, while
    # keeping enough distance from the peak for ordinary option-premium noise.
    if early_triggered and peak_r + 1e-9 >= LOCK_0_TRIGGER_R:
        stage_candidate = entry + LOCK_0_R * risk
        if stage_candidate > new_sl + 1e-9:
            new_sl = stage_candidate
            stage = "LOCK_0_25R_AFTER_0_75R"

    if early_triggered and peak_r + 1e-9 >= LOCK_1_TRIGGER_R:
        stage_candidate = entry + LOCK_1_R * risk
        if stage_candidate > new_sl + 1e-9:
            new_sl = stage_candidate
            stage = "LOCK_0_45R_AFTER_1_00R"

    if early_triggered and peak_r + 1e-9 >= PEAK_PROFIT_RETAIN_TRIGGER_R:
        peak_profit_lock = entry + (
            peak - entry
        ) * PEAK_PROFIT_RETAIN_PERCENT / 100.0
        stage_candidate = max(
            entry + LOCK_2_R * risk,
            peak - 0.70 * risk,
            peak_profit_lock,
        )
        if stage_candidate > new_sl + 1e-9:
            new_sl = stage_candidate
            stage = "LOCK_70PCT_PEAK_PROFIT_AFTER_1_50R"

    if early_triggered and peak_r + 1e-9 >= RUNNER_TRIGGER_R:
        stage_candidate = max(
            entry + RUNNER_MIN_LOCK_R * risk,
            peak - RUNNER_DISTANCE_R * risk,
        )
        if stage_candidate > new_sl + 1e-9:
            new_sl = stage_candidate
            stage = "RUNNER_TRAIL_0_65R_AFTER_2_00R"

    if early_triggered and peak_r + 1e-9 >= SMOOTH_TRAIL_TRIGGER_R:
        stage_candidate = max(
            entry + SMOOTH_TRAIL_MIN_LOCK_R * risk,
            peak - SMOOTH_TRAIL_DISTANCE_R * risk,
        )
        if stage_candidate > new_sl + 1e-9:
            new_sl = stage_candidate
            stage = "SMOOTH_TRAIL_0_55R_AFTER_2_50R"

    if early_triggered and peak_r + 1e-9 >= TIGHT_TRAIL_TRIGGER_R:
        stage_candidate = max(
            entry + TIGHT_TRAIL_MIN_LOCK_R * risk,
            peak - TIGHT_TRAIL_DISTANCE_R * risk,
        )
        if stage_candidate > new_sl + 1e-9:
            new_sl = stage_candidate
            stage = "TIGHT_TRAIL_0_45R_AFTER_4_00R"

    # A protected stop must stay below the observed peak by at least one tick.
    peak_room = _round_down_tick(peak - TICK_SIZE)
    # Before the exact +4% trigger, preserve the initial ATR stop byte-for-byte;
    # even tick-normalisation must not look like an early trailing update.
    desired = _round_up_tick(new_sl) if early_triggered else old_sl
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
        "peak_profit_retained_percent": round(max(
            0.0,
            min(
                100.0,
                (candidate - entry) / max(TICK_SIZE, peak - entry) * 100.0,
            ),
        ), 2),
        "stage": stage,
        "initial_risk": round(risk, 2),
        "cost_safe_breakeven_price": round(cost_cover_price, 2),
        "exact_4pct_solver_price": round(exact_price, 2),
        "breakeven_triggered": early_triggered,
        "four_pct_triggered": four_pct_triggered,
        "expiry_day_trade": expiry_day_trade,
        "expiry_early_floor_min_peak_r": (
            EXPIRY_EARLY_FLOOR_MIN_PEAK_R if expiry_day_trade else None
        ),
        "breakeven_rule": (
            "COST_FLOOR_AT_4PCT_WITH_0_50R_ROOM_AND_R_RATCHET"
        ),
        "breakeven_net_profit_percent": COST_COVER_NET_PROFIT_PERCENT,
        "early_protection_trigger_net_percent": EARLY_PROTECTION_TRIGGER_NET_PERCENT,
        "early_protection_lock_net_percent": EARLY_PROTECTION_LOCK_NET_PERCENT,
        "early_trigger_price": round(early_trigger_price, 2),
        "cost_floor_price": round(cost_floor_price, 2),
        "protected_4pct_price": round(protected_4pct_price, 2),
        # Backward-compatible telemetry alias; it now carries the 4% trigger.
        "protected_2pct_price": round(protected_4pct_price, 2),
        "four_pct_min_lock_net_percent": FOUR_PCT_MIN_LOCK_NET_PERCENT,
        "four_pct_min_lock_price": round(four_pct_min_lock_price, 2),
        "four_pct_min_peak_room_r": FOUR_PCT_MIN_PEAK_ROOM_R,
        "four_pct_room_cap_price": round(four_pct_room_cap_price, 2),
        "four_pct_breathing_lock_price": round(
            four_pct_breathing_lock_price,
            2,
        ),
        "four_pct_full_lock_armed": bool(
            four_pct_breathing_lock_price + 1e-9 >= protected_4pct_price
        ),
        "four_pct_preferred_room_preserved": bool(
            four_pct_breathing_lock_price
            <= four_pct_room_cap_price + 1e-9
        ),
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
            "early_cost_floor_trigger_net_percent": EARLY_PROTECTION_TRIGGER_NET_PERCENT,
            "four_pct_trigger_r": FIRST_LOCK_TRIGGER_R,
            "four_pct_min_peak_room_r": FOUR_PCT_MIN_PEAK_ROOM_R,
            "lock_0_25r_trigger_r": LOCK_0_TRIGGER_R,
            "lock_0_45r_trigger_r": LOCK_1_TRIGGER_R,
            "lock_0_80r_trigger_r": LOCK_2_TRIGGER_R,
            "peak_profit_retain_trigger_r": PEAK_PROFIT_RETAIN_TRIGGER_R,
            "peak_profit_retain_percent": PEAK_PROFIT_RETAIN_PERCENT,
            "runner_trigger_r": RUNNER_TRIGGER_R,
            "runner_distance_r": RUNNER_DISTANCE_R,
            "smooth_trail_trigger_r": SMOOTH_TRAIL_TRIGGER_R,
            "smooth_trail_distance_r": SMOOTH_TRAIL_DISTANCE_R,
            "tight_trail_trigger_r": TIGHT_TRAIL_TRIGGER_R,
            "tight_trail_distance_r": TIGHT_TRAIL_DISTANCE_R,
        },
    }


def _protected_profit_stop_reason(trade, current_price: float, trail) -> str | None:
    """Return the profit-stop reason that must outrank softer exit signals."""
    entry = max(TICK_SIZE, _f(runtime._v(trade, "entry_price", TICK_SIZE)))
    stop = max(TICK_SIZE, _f((trail or {}).get("sl_price"), TICK_SIZE))
    current = max(TICK_SIZE, _f(current_price, TICK_SIZE))
    if stop + 1e-9 < entry or current > stop + 1e-9:
        return None
    return (
        "PROFIT LOCK TRAIL HIT"
        f" | {(trail or {}).get('stage', 'PROTECTED_PROFIT_STOP')}"
        f" | locked={_f((trail or {}).get('locked_r')):.2f}R"
    )


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
    previous_evaluate = runtime._evaluate_exit
    evaluator_is_current = bool(
        getattr(previous_evaluate, "_okai_authoritative_profit_lock_v3", False)
    )
    previous_close = runtime._close
    close_is_current = bool(
        getattr(previous_close, "_okai_paper_stop_fill_v1", False)
    )

    # A later strategy patch can replace the evaluator while leaving the old
    # module-level installed flag set. Only skip when the currently callable
    # exit and PAPER fill functions are still our final wrappers.
    if evaluator_is_current and close_is_current:
        apply_trade_visibility_metrics_patch()
        return

    def evaluate_with_authoritative_trail(trade, ltp, market_data, candle_id):
        result = dict(
            previous_evaluate(trade, ltp, market_data, candle_id) or {}
        )
        trail = _authoritative_trail(trade, ltp)
        result["trail"] = trail
        result["risk"] = trail["initial_risk"]

        previous_updates = _i(runtime._v(trade, "trail_updates", 0), 0)
        result["updates"] = previous_updates + (1 if trail["updated"] else 0)

        reason = result.get("reason")
        reason_text = str(reason or "").upper()
        protected_reason = _protected_profit_stop_reason(trade, ltp, trail)

        if auto_hero.is_auto_hero_zero_trade(trade):
            hero_reason = auto_hero.auto_hero_zero_exit_reason(trade, ltp)
            if hero_reason is not None:
                reason = hero_reason
            elif (
                "SL HIT" not in reason_text
                and "PROFIT LOCK TRAIL HIT" not in reason_text
            ):
                # Hero Zero intentionally ignores ordinary structural/EOD
                # exits inside its short window. Its own 50% SL, profit trail,
                # 2x target and 15:15 hard exit remain authoritative.
                reason = None
                reason_text = ""
            result["reason"] = reason
            result["auto_hero_zero"] = {
                "active": True,
                "paper_only": True,
                "target_multiple": auto_hero.TARGET_MULTIPLE,
                "force_exit_ist": "15:15",
                "version": auto_hero.VERSION,
            }
            if hero_reason is not None:
                result["profit_lock_authority"] = AUTHORITY_VERSION
                return result

        # Once a cost-safe profit stop has latched, it is the hard floor. Softer
        # danger/structural/CAS decisions must not bypass the saved stop and turn
        # an observed winner into a loss. EOD/manual exits above the stop remain
        # untouched; if price is already through the stop, the stop reason wins.
        if protected_reason is not None:
            reason = protected_reason
        else:
            if (
                "PURE ATR SL HIT" in reason_text
                or "PROFIT LOCK TRAIL HIT" in reason_text
            ):
                reason = None
            if reason is None and _f(ltp) <= _f(trail.get("sl_price")):
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
    if not evaluator_is_current:
        runtime._evaluate_exit = evaluate_with_authoritative_trail
    if not close_is_current:
        runtime._close = close_with_bounded_paper_stop_fill
    runtime._okai_authoritative_profit_lock_v3 = True
    runtime._okai_authoritative_profit_lock_v2 = True
    apply_trade_visibility_metrics_patch()
