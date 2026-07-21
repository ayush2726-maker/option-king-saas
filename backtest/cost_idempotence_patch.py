"""Make backtest cost application idempotent.

AUTO runs through both the public run_realistic_day_backtest wrapper and the
AUTO portfolio wrapper. Without this guard, slippage/brokerage/statutory charges
can be deducted twice from the same already-costed result.

The same startup hook activates the Angel historical index-token patch and,
after the single cost model is ready, the true cost-safe break-even and all-in
hard risk controls.
"""

from copy import deepcopy

from backtest import realism_costs_patch as costs
from backtest.angel_historical_index_patch import (
    apply_angel_historical_index_patch,
)


def _activate_true_be_and_risk_caps():
    from backtest.cost_safe_breakeven_risk_patch import (
        apply_cost_safe_breakeven_risk_patch,
    )
    from backtest.all_in_risk_cap_patch import (
        apply_all_in_risk_cap_patch,
    )

    apply_cost_safe_breakeven_risk_patch()
    apply_all_in_risk_cap_patch()


def apply_cost_idempotence_patch():
    # main.py calls this after apply_live_frequency_portfolio_patch(), so this
    # remains the safe place to replace only the historical candle helper.
    apply_angel_historical_index_patch()

    if getattr(costs, "_okai_cost_idempotence_v1", False):
        # A warm worker may already have cost idempotence while the newer true
        # break-even/risk patches have not yet been installed.
        _activate_true_be_and_risk_caps()
        return

    original = costs._apply_costs_to_result

    def idempotent_costs(result, broker_name, fallback_instrument=None):
        if isinstance(result, dict) and result.get("costs_applied"):
            output = deepcopy(result)
            output["cost_application_count"] = 1
            output["cost_idempotence_guard"] = "ALREADY_COSTED_SKIP_SECOND_PASS"
            summary = dict(output.get("summary") or {})
            summary["cost_application_count"] = 1
            output["summary"] = summary
            return output

        output = original(result, broker_name, fallback_instrument)
        if isinstance(output, dict) and output.get("costs_applied"):
            output["cost_application_count"] = 1
            output["cost_idempotence_guard"] = "FIRST_AND_ONLY_COST_PASS"
            summary = dict(output.get("summary") or {})
            summary["cost_application_count"] = 1
            output["summary"] = summary
        return output

    costs._apply_costs_to_result = idempotent_costs
    costs._okai_cost_idempotence_v1 = True
    _activate_true_be_and_risk_caps()
