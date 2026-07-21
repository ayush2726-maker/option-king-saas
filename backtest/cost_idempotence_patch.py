"""Make backtest cost application idempotent and activate final parity patches.

AUTO runs through both the public run_realistic_day_backtest wrapper and the
AUTO portfolio wrapper. Without this guard, slippage/brokerage/statutory charges
can be deducted twice from the same already-costed result.

This startup hook also activates:
- historical index-token safety;
- true cost-safe break-even and the quantity-preserving 8% premium stop cap;
- exact Paper/Live net P&L and exact trade-cost break-even; and
- the maximum five Backtest trades per trading day.
"""

from copy import deepcopy

from backtest import realism_costs_patch as costs
from backtest.angel_historical_index_patch import (
    apply_angel_historical_index_patch,
)


def _activate_final_parity_patches():
    from backtest.cost_safe_breakeven_risk_patch import (
        apply_cost_safe_breakeven_risk_patch,
    )
    from backtest.all_in_risk_cap_patch import (
        apply_all_in_risk_cap_patch,
    )
    from bot.live_net_pnl_breakeven_patch import (
        apply_live_net_pnl_breakeven_patch,
    )
    from backtest.daily_trade_limit_patch import (
        apply_daily_trade_limit_patch,
    )

    # Order matters: cost/risk wrappers must be final before the daily result is
    # capped, and runtime structural-exit patches are already active in main.py.
    apply_cost_safe_breakeven_risk_patch()
    apply_all_in_risk_cap_patch()
    apply_live_net_pnl_breakeven_patch()
    apply_daily_trade_limit_patch()


def apply_cost_idempotence_patch():
    # main.py calls this after live portfolio, structural exit and realism costs.
    apply_angel_historical_index_patch()

    if getattr(costs, "_okai_cost_idempotence_v1", False):
        # Warm workers may already have cost idempotence while newer final
        # parity patches have not yet been installed.
        _activate_final_parity_patches()
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
    _activate_final_parity_patches()
