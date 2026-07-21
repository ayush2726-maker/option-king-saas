"""Stable live/backtest strategy parity.

V3 deliberately does not replace FastAPI route endpoints and does not depend on
request ContextVars.  The earlier endpoint wrapper broke both Daily and Monthly
backtests.  Backtest routes remain untouched; only their module-level signal
function is routed through the final live Default 82 pipeline.

The permanent OKAI Default 82 profile currently is:
VWAP 15, Supertrend 13, EMA trend 18, ORB 15, momentum 10,
ADX 14, volume 5 and MTF 10.  The final live pipeline additionally requires
VWAP + Supertrend + EMA direction and keeps fresh-entry/anti-chase guards.
"""

from starlette.middleware.base import BaseHTTPMiddleware

from backtest import routes as backtest_routes
from bot import strategy as live_strategy
from bot.default_strategy_patch import _default_profile


class BacktestActiveStrategyMiddleware(BaseHTTPMiddleware):
    """Compatibility pass-through retained because main.py imports this class."""

    async def dispatch(self, request, call_next):
        return await call_next(request)


def _safe_error_signal(exc):
    return {
        "signal": "WAIT",
        "candidate_signal": "WAIT",
        "score": 0,
        "min_score": 82,
        "trade_allowed": False,
        "warnings": [
            "BACKTEST_LIVE_SIGNAL_ERROR:" + exc.__class__.__name__,
        ],
        "strategy_consistency_error": str(exc)[:240],
    }


def apply_backtest_live_strategy_patch():
    if getattr(backtest_routes, "_okai_live_backtest_parity_v3", False):
        return

    # Do not wrap run_backtest, monthly worker or FastAPI route endpoints.
    # Those original routes were working before the V1/V2 wrapper.
    def current_live_default_signal(
        market_data,
        consecutive_losses=0,
        profile=None,
    ):
        selected_profile = profile or _default_profile()
        try:
            return live_strategy.get_full_signal(
                market_data,
                consecutive_losses=consecutive_losses,
                profile=selected_profile,
            )
        except Exception as exc:
            # A malformed candle must not abort an entire day/month test.
            return _safe_error_signal(exc)

    # Every Normal, AUTO, Monthly and Hero Zero calculation resolves this
    # module global at runtime, including the captured single-index function.
    backtest_routes.get_full_signal = current_live_default_signal

    # The final live signal already enforces score 82 plus mandatory
    # VWAP/Supertrend/EMA.  The old extra 4-of-5 gate caused disagreement.
    backtest_routes._OKAI_NORMAL_MIN_CORE_CONFIRMATIONS = 0

    backtest_routes._okai_live_backtest_parity_v1 = True
    backtest_routes._okai_live_backtest_parity_v2 = True
    backtest_routes._okai_live_backtest_parity_v3 = True
