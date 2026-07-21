"""Keep backtests aligned with the currently active live/paper strategy.

The backtest module imported ``get_full_signal`` before startup patches were
installed, so it retained an older scoring function and its legacy 4-of-5 label.
This patch routes every backtest signal through the final live strategy pipeline
and supplies the requesting user's active Strategy Builder profile.
"""

from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware

from backtest import routes as backtest_routes
from bot import strategy as live_strategy
from strategy.profile_engine import get_active_profile_config


_active_backtest_profile = ContextVar(
    "okai_active_backtest_profile",
    default=None,
)


class BacktestActiveStrategyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not request.url.path.startswith("/backtest/"):
            return await call_next(request)

        token = None
        try:
            authorization = request.headers.get("authorization")
            user = backtest_routes.get_current_user(authorization)
            profile = get_active_profile_config(user["id"])
            token = _active_backtest_profile.set(profile)
            request.state.okai_backtest_profile = profile
        except Exception:
            # The endpoint itself will return the normal authentication/profile
            # error.  Never silently substitute another user's profile.
            token = _active_backtest_profile.set(None)

        try:
            return await call_next(request)
        finally:
            if token is not None:
                _active_backtest_profile.reset(token)


def _profile_metadata(profile):
    profile = profile or {}
    weights = dict(profile.get("weights") or {})
    return {
        "profile_key": profile.get("profile_key", "okai_default_82"),
        "profile_name": profile.get("profile_name", "OKAI Default 82"),
        "entry_threshold": int(profile.get("entry_threshold", 82) or 82),
        "weights": weights,
        "mandatory_confirmations": [
            "VWAP_DIRECTION",
            "SUPERTREND_DIRECTION",
            "EMA9_EMA21_TREND",
        ],
        "signal_pipeline": "SAME_AS_LIVE_MANDATORY_VWAP_ST_EMA_V1",
    }


def _decorate_result(result):
    if not isinstance(result, dict):
        return result

    profile = _active_backtest_profile.get()
    metadata = _profile_metadata(profile)
    result["strategy_profile"] = metadata
    result["strategy_consistency"] = {
        "same_signal_pipeline_as_live": True,
        "profile_loaded_from_active_strategy": bool(profile),
        "mode": "LIVE_BACKTEST_PARITY_V1",
    }
    result["entry_quality_gate"] = {
        "entry_score": metadata["entry_threshold"],
        "mandatory_confirmations": metadata["mandatory_confirmations"],
        "mandatory_count": 3,
        "orb_compulsory": False,
        "two_candle_momentum_compulsory": False,
        "mode": "SCORE82_PLUS_VWAP_ST_EMA_ALL_REQUIRED",
    }

    summary = dict(result.get("summary") or {})
    summary["strategy_profile"] = metadata["profile_name"]
    summary["strategy_consistency"] = "SAME_AS_LIVE"
    result["summary"] = summary
    return result


def apply_backtest_live_strategy_patch():
    if getattr(backtest_routes, "_okai_live_backtest_parity_v1", False):
        return

    original_day_backtest = backtest_routes.run_realistic_day_backtest

    def current_live_signal(market_data, consecutive_losses=0, profile=None):
        selected_profile = profile or _active_backtest_profile.get()
        return live_strategy.get_full_signal(
            market_data,
            consecutive_losses=consecutive_losses,
            profile=selected_profile,
        )

    def consistent_day_backtest(*args, **kwargs):
        return _decorate_result(original_day_backtest(*args, **kwargs))

    # All normal and Hero Zero calculations resolve this module global at runtime.
    backtest_routes.get_full_signal = current_live_signal
    backtest_routes.run_realistic_day_backtest = consistent_day_backtest

    # AUTO mode captured the original single-index function in a module constant.
    if hasattr(backtest_routes, "_OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST"):
        backtest_routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST = consistent_day_backtest

    backtest_routes._okai_live_backtest_parity_v1 = True
