"""Keep every backtest on the same active strategy as the live bot.

V2 removes the request-middleware dependency. Monthly backtests execute in a
background thread, so ContextVar state created by HTTP middleware is not
available there. The active profile is now loaded inside the real Daily and
Monthly execution functions. The legacy extra 4-of-5 backtest gate is also
disabled because the final live signal pipeline already enforces score 82 plus
mandatory VWAP, Supertrend and EMA trend alignment.
"""

from contextvars import ContextVar
import traceback

from starlette.middleware.base import BaseHTTPMiddleware

from backtest import routes as backtest_routes
from bot import strategy as live_strategy
from bot.default_strategy_patch import _default_profile
from strategy.profile_engine import get_active_profile_config


_active_backtest_profile = ContextVar(
    "okai_active_backtest_profile_v2",
    default=None,
)


class BacktestActiveStrategyMiddleware(BaseHTTPMiddleware):
    """Compatibility pass-through; V2 loads profiles inside execution functions."""

    async def dispatch(self, request, call_next):
        return await call_next(request)


def _load_profile(authorization):
    user = backtest_routes.get_current_user(authorization)
    return get_active_profile_config(user["id"])


def _profile_metadata(profile):
    profile = profile or _default_profile()
    return {
        "profile_key": profile.get("profile_key", "okai_default_82"),
        "profile_name": profile.get("profile_name", "OKAI Default 82"),
        "entry_threshold": int(profile.get("entry_threshold", 82) or 82),
        "weights": dict(profile.get("weights") or {}),
        "mandatory_confirmations": [
            "VWAP_DIRECTION",
            "SUPERTREND_DIRECTION",
            "EMA9_EMA21_TREND",
        ],
        "signal_pipeline": "SAME_AS_LIVE_MANDATORY_VWAP_ST_EMA_V2",
    }


def _decorate_result(result, profile=None):
    if not isinstance(result, dict):
        return result

    selected = profile or _active_backtest_profile.get() or _default_profile()
    metadata = _profile_metadata(selected)
    result["strategy_profile"] = metadata
    result["strategy_consistency"] = {
        "same_signal_pipeline_as_live": True,
        "profile_loaded_from_active_strategy": bool(
            profile or _active_backtest_profile.get()
        ),
        "mode": "LIVE_BACKTEST_PARITY_V2",
    }
    result["entry_quality_gate"] = {
        "entry_score": metadata["entry_threshold"],
        "mandatory_confirmations": metadata["mandatory_confirmations"],
        "mandatory_count": 3,
        "orb_compulsory": False,
        "two_candle_momentum_compulsory": False,
        "legacy_core_4_of_5_enabled": False,
        "mode": "SCORE82_PLUS_VWAP_ST_EMA_ALL_REQUIRED",
    }

    summary = dict(result.get("summary") or {})
    summary["strategy_profile"] = metadata["profile_name"]
    summary["strategy_consistency"] = "SAME_AS_LIVE"
    result["summary"] = summary
    return result


def _run_with_profile(profile, function, *args, **kwargs):
    token = _active_backtest_profile.set(profile)
    try:
        return function(*args, **kwargs)
    finally:
        _active_backtest_profile.reset(token)


def _replace_router_endpoint(path, replacement):
    """Replace FastAPI's cached endpoint call before router inclusion."""
    for route in getattr(backtest_routes.router, "routes", []):
        if getattr(route, "path", None) != path:
            continue
        route.endpoint = replacement
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            dependant.call = replacement


def apply_backtest_live_strategy_patch():
    if getattr(backtest_routes, "_okai_live_backtest_parity_v2", False):
        return

    original_signal = backtest_routes.get_full_signal
    original_day_backtest = backtest_routes.run_realistic_day_backtest
    original_run_endpoint = backtest_routes.run_backtest
    original_monthly_sync = backtest_routes._okai_run_monthly_backtest_sync

    def current_live_signal(market_data, consecutive_losses=0, profile=None):
        selected_profile = (
            profile
            or _active_backtest_profile.get()
            or _default_profile()
        )
        try:
            return live_strategy.get_full_signal(
                market_data,
                consecutive_losses=consecutive_losses,
                profile=selected_profile,
            )
        except Exception as exc:
            # One malformed snapshot must not abort an entire day/month.
            fallback = original_signal(
                market_data,
                consecutive_losses=consecutive_losses,
            )
            if isinstance(fallback, dict):
                fallback = dict(fallback)
                warnings = list(fallback.get("warnings") or [])
                warnings.append(
                    "LIVE_SIGNAL_FALLBACK:" + exc.__class__.__name__
                )
                fallback["warnings"] = warnings
                fallback["strategy_consistency_error"] = str(exc)[:240]
                fallback["trade_allowed"] = False
                fallback["signal"] = "WAIT"
            return fallback

    def consistent_day_backtest(*args, **kwargs):
        result = original_day_backtest(*args, **kwargs)
        return _decorate_result(result)

    def daily_endpoint(body: dict, authorization=None):
        try:
            profile = _load_profile(authorization)
            result = _run_with_profile(
                profile,
                original_run_endpoint,
                body,
                authorization,
            )
            return _decorate_result(result, profile)
        except Exception as exc:
            return {
                "success": False,
                "message": "Backtest run failed, but exact error is visible.",
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "error_stage": "ACTIVE_PROFILE_OR_DAILY_EXECUTION",
                "trace": traceback.format_exc()[-1200:],
            }

    def monthly_sync(body: dict, authorization=None):
        try:
            profile = _load_profile(authorization)
            result = _run_with_profile(
                profile,
                original_monthly_sync,
                body,
                authorization,
            )
            return _decorate_result(result, profile)
        except Exception as exc:
            return {
                "success": False,
                "message": "Monthly backtest failed, but exact error is visible.",
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "error_stage": "ACTIVE_PROFILE_OR_MONTHLY_EXECUTION",
                "trace": traceback.format_exc()[-1200:],
            }

    backtest_routes.get_full_signal = current_live_signal
    backtest_routes.run_realistic_day_backtest = consistent_day_backtest
    backtest_routes.run_backtest = daily_endpoint
    backtest_routes._okai_run_monthly_backtest_sync = monthly_sync

    # AUTO mode captured the single-index function in a module constant.
    if hasattr(backtest_routes, "_OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST"):
        backtest_routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST = consistent_day_backtest

    # Live mandatory structure already replaces the old extra 4-of-5 rule.
    backtest_routes._OKAI_NORMAL_MIN_CORE_CONFIRMATIONS = 0

    _replace_router_endpoint("/backtest/run", daily_endpoint)

    backtest_routes._okai_live_backtest_parity_v1 = True
    backtest_routes._okai_live_backtest_parity_v2 = True
