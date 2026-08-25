"""Final capital ceiling plus planned-stop risk sizing.

Strategy quality, stops, costs and post-loss guards remain active.  This patch only
keeps the configured capital allocation as an affordability ceiling, while
preventing a normal trade's planned ATR stop from risking a large part of the
account:

- Runtime AUTO keeps slot 1 = 50% and slot 2 = 40% of current capital.
- Backtest AUTO keeps its configured capital allocation (including CAP90).
- Runtime lot quantity is capped at 10% of current equity using the exact
  planned option ATR-stop distance.
- Backtests use the conservative 15% premium stop cap when the exact live ATR
  context is unavailable.
"""

import math

from backtest import routes as backtest_routes
from backtest.range_capital_mode_patch import apply_range_capital_mode_patch
from bot import auto_portfolio_runtime as runtime


NORMAL_MAX_PLANNED_LOSS_PERCENT = 10.0
BACKTEST_CONSERVATIVE_PREMIUM_RISK_PERCENT = 15.0


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _i(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def _runtime_capital_size(
    capital_base,
    slot,
    premium,
    lot_size,
    rows=None,
    risk_points=None,
):
    """Size against the configured slot without breaking the runtime API.

    ``auto_portfolio_runtime._open_common`` passes the currently open rows so
    the sizing layer can preserve the portfolio reserve.  The old final patch
    dropped that keyword from its signature, which raised ``TypeError`` before
    every otherwise-qualified entry could be inserted.

    Slots 1 and 2 keep their configured 50/40 targets.  Slot 3 may use one
    complete lot only when rounding in the first two positions left capital
    above the hard reserve; this removes the impossible zero-budget slot while
    keeping the reserve intact.
    """
    capital = max(0.0, _f(capital_base))
    price = max(0.0, _f(premium))
    lot = max(1, _i(lot_size, 1))
    slot_number = _i(slot, 1)
    allocation = float(runtime.SLOT_ALLOCATIONS.get(slot_number, 0.0))
    target_budget = max(0.0, capital * allocation)
    reserve_floor = max(0.0, capital * runtime.RESERVE_ALLOCATION)
    committed = sum(runtime._row_capital_used(row) for row in (rows or []))
    available_after_reserve = max(0.0, capital - reserve_floor - committed)
    one_lot_cost = price * lot

    # Slot 3 is the optional remainder slot.  Never consume the 10% reserve,
    # and never scale it beyond the minimum complete exchange lot.
    flex_used = bool(
        slot_number == 3
        and one_lot_cost > 0
        and one_lot_cost <= available_after_reserve + 1e-9
    )
    budget = (
        one_lot_cost
        if flex_used
        else min(target_budget, available_after_reserve)
    )
    affordability_lots = (
        int(math.floor(budget / one_lot_cost))
        if one_lot_cost > 0
        else 0
    )
    planned_risk = (
        max(0.05, _f(risk_points, 0.05))
        if risk_points is not None
        else None
    )
    risk_budget = capital * NORMAL_MAX_PLANNED_LOSS_PERCENT / 100.0
    planned_risk_per_lot = (
        planned_risk * lot
        if planned_risk is not None
        else None
    )
    risk_lots = (
        int(math.floor((risk_budget + 1e-9) / planned_risk_per_lot))
        if planned_risk_per_lot is not None and planned_risk_per_lot > 0
        else affordability_lots
    )
    lots = min(affordability_lots, max(0, risk_lots))
    qty = lots * lot
    capital_used = round(price * qty, 2)
    return {
        "lot_size": lot,
        "lots": lots,
        "qty": qty,
        "target_slot_budget": round(target_budget, 2),
        "slot_budget": round(budget, 2),
        "usable_capital": round(budget, 2),
        "reserve_floor": round(reserve_floor, 2),
        "committed_capital": round(committed, 2),
        "available_after_reserve": round(available_after_reserve, 2),
        "one_lot_cost": round(one_lot_cost, 2),
        "capital_used": capital_used,
        "capital_left_in_slot": round(max(0.0, budget - capital_used), 2),
        "allocation_percent": round(allocation * 100.0, 2),
        "actual_allocation_pct": round(
            capital_used / capital * 100.0 if capital > 0 else 0.0,
            2,
        ),
        "flex_used": flex_used,
        "sizing_mode": (
            "REMAINDER_SLOT_ONE_LOT"
            if flex_used
            else "CAPITAL_BASED_ALLOCATION"
        ),
        "risk_cap_applied": lots < affordability_lots,
        "risk_sizing_mode": (
            "NORMAL_PLANNED_SL_LOSS_CAP_10PCT"
            if planned_risk is not None
            else "CAPITAL_BASED_ALLOCATION_NO_RISK_CONTEXT"
        ),
        "quantity_sizing_rule": (
            "MIN_CAPITAL_ALLOCATION_AND_10PCT_PLANNED_SL_RISK"
            if planned_risk is not None
            else "FLOOR_ALLOCATION_DIVIDED_BY_PREMIUM_AND_LOT"
        ),
        "max_planned_loss_percent": (
            NORMAL_MAX_PLANNED_LOSS_PERCENT
            if planned_risk is not None
            else None
        ),
        "max_planned_loss_amount": (
            round(risk_budget, 2) if planned_risk is not None else None
        ),
        "planned_risk_points": (
            round(planned_risk, 2) if planned_risk is not None else None
        ),
        "planned_risk_per_lot": (
            round(planned_risk_per_lot, 2)
            if planned_risk_per_lot is not None
            else None
        ),
        "affordability_lots": affordability_lots,
        "risk_lots": risk_lots,
    }


def _backtest_capital_size(capital, premium, lot_size, allocation):
    capital_value = max(0.0, _f(capital))
    price = max(0.0, _f(premium))
    lot = max(1, _i(lot_size, 1))
    allocation_value = max(0.0, min(1.0, _f(allocation)))
    budget = capital_value * allocation_value
    one_lot_cost = price * lot
    affordability_lots = (
        int(math.floor(budget / one_lot_cost))
        if one_lot_cost > 0
        else 0
    )
    risk_budget = capital_value * NORMAL_MAX_PLANNED_LOSS_PERCENT / 100.0
    planned_risk_points = price * BACKTEST_CONSERVATIVE_PREMIUM_RISK_PERCENT / 100.0
    planned_risk_per_lot = planned_risk_points * lot
    risk_lots = (
        int(math.floor((risk_budget + 1e-9) / planned_risk_per_lot))
        if planned_risk_per_lot > 0
        else 0
    )
    lots = min(affordability_lots, max(0, risk_lots))
    quantity = lots * lot
    capital_used = round(price * quantity, 2)
    return {
        "lots": lots,
        "quantity": quantity,
        "qty": quantity,
        "lot_size": lot,
        "allocation": allocation_value,
        "allocation_percent": round(allocation_value * 100.0, 2),
        "allocated_capital": round(budget, 2),
        "usable_capital": round(budget, 2),
        "one_lot_cost": round(one_lot_cost, 2),
        "capital_used": capital_used,
        "used_capital": capital_used,
        "capital_left": round(max(0.0, budget - capital_used), 2),
        "capital_utilization_percent": round(
            capital_used / max(0.01, capital_value) * 100.0,
            2,
        ),
        "slot_utilization_percent": round(
            capital_used / max(0.01, budget) * 100.0,
            2,
        ) if budget > 0 else 0.0,
        "affordable": lots >= 1,
        "risk_cap_applied": lots < affordability_lots,
        "quantity_risk_cap_enabled": True,
        "quantity_preserved": lots == affordability_lots,
        "risk_sizing_mode": "CONSERVATIVE_PLANNED_SL_LOSS_CAP_10PCT",
        "quantity_sizing_rule": "MIN_ALLOCATION_AND_10PCT_PLANNED_SL_RISK",
        "max_planned_loss_percent": NORMAL_MAX_PLANNED_LOSS_PERCENT,
        "max_planned_loss_amount": round(risk_budget, 2),
        "planned_risk_points": round(planned_risk_points, 2),
        "planned_risk_per_lot": round(planned_risk_per_lot, 2),
        "affordability_lots": affordability_lots,
        "risk_lots": risk_lots,
    }


def _annotate_result(result):
    if not isinstance(result, dict):
        return result
    output = result
    metadata = {
        "quantity_risk_cap_enabled": True,
        "position_sizing_mode": "CAPITAL_CEILING_PLUS_10PCT_PLANNED_SL_RISK",
        "capital_use_rule": "MIN_ALLOCATION_AND_10PCT_PLANNED_SL_RISK",
    }
    output.update(metadata)
    sizing = dict(output.get("position_sizing") or {})
    sizing.update({
        "mode": "CAPITAL_CEILING_PLUS_10PCT_PLANNED_SL_RISK",
        "slot_1_allocation_percent": 50,
        "slot_2_allocation_percent": 40,
        "auto_backtest_capital_use_percent": 90,
        "equity_risk_lot_cap": True,
        "max_planned_loss_percent": NORMAL_MAX_PLANNED_LOSS_PERCENT,
    })
    output["position_sizing"] = sizing
    summary = dict(output.get("summary") or {})
    summary.update(metadata)
    output["summary"] = summary
    output["note"] = (
        "Quantity keeps the capital allocation ceiling and is additionally "
        "capped so planned stop loss is at most 10% of current equity."
    )
    return output


def apply_capital_based_sizing_restore_patch():
    """Install after all portfolio/risk wrappers so it is the final sizing rule."""
    runtime._size = _runtime_capital_size
    runtime._okai_risk_sizing_v2 = True
    runtime._okai_capital_based_sizing_final = True

    backtest_routes._okai_calculate_lot_sizing = _backtest_capital_size
    backtest_routes._okai_risk_sizing_v2 = True
    backtest_routes._okai_capital_based_sizing_final = True

    original_auto = getattr(backtest_routes, "_okai_run_auto_index_backtest", None)
    if callable(original_auto) and not getattr(
        backtest_routes,
        "_okai_capital_sizing_result_annotation_v1",
        False,
    ):
        def capital_annotated_auto(*args, **kwargs):
            return _annotate_result(original_auto(*args, **kwargs))

        backtest_routes._okai_run_auto_index_backtest = capital_annotated_auto
        backtest_routes._okai_capital_sizing_result_annotation_v1 = True

    # Date-range comparison uses this same capital-based lot logic.  Apply it
    # here, after the final daily/AUTO backtest dispatcher has been installed.
    apply_range_capital_mode_patch()
