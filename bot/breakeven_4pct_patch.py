"""Use charges plus 4% net profit as the first cost-safe profit lock.

The active runtime and backtest profit-lock functions are already wrapped by the
expectancy engine. This patch is therefore applied after that engine: it changes
the underlying exact-cost solvers to 4% and normalises the final metadata without
removing any runner/trailing behaviour.
"""

from __future__ import annotations

from backtest import cost_safe_breakeven_risk_patch as backtest_cost
from backtest import routes as backtest_routes
from bot import angel_fetcher
from bot import dynamic_exit
from bot import live_net_pnl_breakeven_patch as live_cost
from bot import strategy
from bot.authoritative_profit_lock_runtime_patch import (
    apply_authoritative_profit_lock_runtime_patch,
)


NET_PROFIT_LOCK_PERCENT = 4.0


def _normalise(result):
    output = dict(result or {})
    for key in ("stage", "breakeven_rule"):
        value = output.get(key)
        if isinstance(value, str):
            output[key] = (
                value.replace("2PCT", "4PCT")
                .replace("5PCT", "4PCT")
                .replace("2_PERCENT", "4_PERCENT")
                .replace("5_PERCENT", "4_PERCENT")
            )
    output["breakeven_net_profit_percent"] = NET_PROFIT_LOCK_PERCENT
    return output


def _wrap(base):
    if not callable(base) or getattr(base, "_okai_breakeven_4pct_v1", False):
        return base

    def four_percent_lock(
        entry_price,
        initial_risk,
        current_sl,
        peak_price,
        current_price,
    ):
        return _normalise(
            base(
                entry_price,
                initial_risk,
                current_sl,
                peak_price,
                current_price,
            )
        )

    four_percent_lock._okai_breakeven_4pct_v1 = True
    four_percent_lock.__name__ = getattr(base, "__name__", "four_percent_lock")
    return four_percent_lock


def apply_breakeven_4pct_patch() -> None:
    if getattr(angel_fetcher, "_okai_breakeven_4pct_v1", False):
        return

    live_cost.NET_PROFIT_LOCK_PERCENT = NET_PROFIT_LOCK_PERCENT
    backtest_cost.NET_PROFIT_LOCK_PERCENT = NET_PROFIT_LOCK_PERCENT

    try:
        live_cost.calculate_exact_breakeven_price.cache_clear()
    except Exception:
        pass
    try:
        backtest_cost.calculate_cost_safe_breakeven_price.cache_clear()
    except Exception:
        pass

    runtime_lock = _wrap(getattr(angel_fetcher, "_dynamic_profit_lock", None))
    if callable(runtime_lock):
        angel_fetcher._dynamic_profit_lock = runtime_lock
        angel_fetcher.update_option_profit_lock = runtime_lock
        dynamic_exit.update_option_profit_lock = runtime_lock
        strategy.update_option_profit_lock = runtime_lock

    backtest_lock = _wrap(getattr(backtest_routes, "update_option_profit_lock", None))
    if callable(backtest_lock):
        backtest_routes.update_option_profit_lock = backtest_lock

    # Final runtime authority: recalculate the stop from the actual broker, index,
    # quantity and PAPER/LIVE mode. This repairs any stale legacy helper chain while
    # keeping the agreed 0.75R / 1.35R / 2.20R / 3.20R schedule unchanged.
    apply_authoritative_profit_lock_runtime_patch()

    angel_fetcher._okai_breakeven_4pct_v1 = True
