"""Make backtest cost application idempotent.

AUTO runs through both the public run_realistic_day_backtest wrapper and the
AUTO portfolio wrapper. Without this guard, slippage/brokerage/statutory charges
can be deducted twice from the same already-costed result.
"""

from copy import deepcopy

from backtest import realism_costs_patch as costs


def apply_cost_idempotence_patch():
    if getattr(costs, "_okai_cost_idempotence_v1", False):
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
