"""Install Risk Control V2 at the correct runtime/backtest patch order."""

from backtest import live_frequency_portfolio_patch as frequency_patch
from backtest import routes as backtest_routes
from bot.risk_control_v2_patch import (
    _install_backtest_risk_sizing,
    apply_risk_control_v2_patch,
)


if not getattr(frequency_patch, "_okai_risk_control_v2_hook", False):
    _original_frequency_apply = (
        frequency_patch.apply_live_frequency_portfolio_patch
    )

    def apply_frequency_then_risk_control():
        _original_frequency_apply()
        # The frequency patch replaces the AUTO dispatcher. Re-wrap the final
        # dispatcher so backtest lot sizing remains identical to live/paper.
        backtest_routes._okai_risk_sizing_v2 = False
        _install_backtest_risk_sizing()

    frequency_patch.apply_live_frequency_portfolio_patch = (
        apply_frequency_then_risk_control
    )
    frequency_patch._okai_risk_control_v2_hook = True


apply_risk_control_v2_patch()
