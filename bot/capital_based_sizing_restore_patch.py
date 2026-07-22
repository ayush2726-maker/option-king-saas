"""Restore the user's capital-based position sizing as the final sizing layer.

Strategy quality, stops, costs and post-loss guards remain active.  This patch only
removes equity-risk lot caps that silently reduced the quantity selected by the
configured capital allocation:

- Runtime AUTO keeps slot 1 = 50% and slot 2 = 40% of current capital.
- Backtest AUTO keeps its configured capital allocation (including CAP90).
- Lot quantity is floor(allocation / option premium / exchange lot size).
"""

import math

from backtest import routes as backtest_routes
from bot import auto_portfolio_runtime as runtime


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


def _runtime_capital_size(capital_base, slot, premium, lot_size):
    capital = max(0.0, _f(capital_base))
    price = max(0.0, _f(premium))
    lot = max(1, _i(lot_size, 1))
    allocation = float(runtime.SLOT_ALLOCATIONS.get(_i(slot, 1), 0.0))
    budget = max(0.0, capital * allocation)
    one_lot_cost = price * lot
    lots = int(math.floor(budget / one_lot_cost)) if one_lot_cost > 0 else 0
    qty = lots * lot
    return {
        "lot_size": lot,
        "lots": lots,
        "qty": qty,
        "slot_budget": round(budget, 2),
        "capital_used": round(price * qty, 2),
        "allocation_percent": round(allocation * 100.0, 2),
        "risk_cap_applied": False,
        "risk_sizing_mode": "CAPITAL_BASED_ALLOCATION",
        "quantity_sizing_rule": "FLOOR_ALLOCATION_DIVIDED_BY_PREMIUM_AND_LOT",
    }


def _backtest_capital_size(capital, premium, lot_size, allocation):
    capital_value = max(0.0, _f(capital))
    price = max(0.0, _f(premium))
    lot = max(1, _i(lot_size, 1))
    allocation_value = max(0.0, min(1.0, _f(allocation)))
    budget = capital_value * allocation_value
    one_lot_cost = price * lot
    lots = int(math.floor(budget / one_lot_cost)) if one_lot_cost > 0 else 0
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
        "capital_used": capital_used,
        "used_capital": capital_used,
        "capital_utilization_percent": round(
            capital_used / max(0.01, capital_value) * 100.0,
            2,
        ),
        "affordable": lots >= 1,
        "risk_cap_applied": False,
        "quantity_risk_cap_enabled": False,
        "quantity_preserved": True,
        "risk_sizing_mode": "CAPITAL_BASED_ALLOCATION",
        "quantity_sizing_rule": "PRESERVE_CAP90_OR_PORTFOLIO_ALLOCATION",
    }


def _annotate_result(result):
    if not isinstance(result, dict):
        return result
    output = result
    metadata = {
        "quantity_risk_cap_enabled": False,
        "quantity_preserved": True,
        "position_sizing_mode": "CAPITAL_BASED_ALLOCATION",
        "capital_use_rule": "PRESERVE_CAP90_OR_50_40_PORTFOLIO_ALLOCATION",
    }
    output.update(metadata)
    sizing = dict(output.get("position_sizing") or {})
    sizing.update({
        "mode": "CAPITAL_BASED_ALLOCATION",
        "slot_1_allocation_percent": 50,
        "slot_2_allocation_percent": 40,
        "auto_backtest_capital_use_percent": 90,
        "equity_risk_lot_cap": False,
    })
    output["position_sizing"] = sizing
    summary = dict(output.get("summary") or {})
    summary.update(metadata)
    output["summary"] = summary
    output["note"] = (
        "Quantity is selected only from current capital allocation and option "
        "premium. Strategy exits and the 8% premium stop remain separate."
    )
    return output


def apply_capital_based_sizing_restore_patch():
    """Install after all portfolio/risk wrappers so it is the final sizing rule."""
    runtime._size = _runtime_capital_size
    runtime._okai_risk_sizing_v2 = False
    runtime._okai_capital_based_sizing_final = True

    backtest_routes._okai_calculate_lot_sizing = _backtest_capital_size
    backtest_routes._okai_risk_sizing_v2 = False
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
