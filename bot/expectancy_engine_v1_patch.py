"""Expectancy Engine V1: cut weak trades early and give strong winners room.

The June result has a good win rate but negative P&L, which means average losses
are too large compared with average wins.  This patch changes exits, not quantity:

1. Weak-trade invalidation
   - immediate exit at -0.35R when two of VWAP/Supertrend/EMA are opposite;
   - after two completed candles, exit when peak stayed below +0.25R and two of
     the three structure checks are opposite.
2. Controlled runner profit lock
   - true cost-safe break-even only after a meaningful +0.75R move;
   - +1.35R locks +0.50R;
   - +2.20R locks at least +1.20R and trails 1.00R behind peak;
   - +3.20R locks at least +2.00R and trails 1.20R behind peak.
3. Historical weak ATR-stop trades that never reached +0.25R are modeled with a
   -0.45R soft invalidation instead of waiting for the full premium stop.

Capital-based lots, score 82, costs and the 8% hard premium stop are unchanged.
"""

from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime
from bot import dynamic_exit
from bot import strategy
from backtest import routes as backtest_routes
from backtest import cost_safe_breakeven_risk_patch as backtest_cost


TICK_SIZE = 0.05
FAST_WEAKNESS_EXIT_R = -0.35
NO_FOLLOW_THROUGH_BARS = 2
NO_FOLLOW_THROUGH_MAX_PEAK_R = 0.25
BACKTEST_SOFT_INVALIDATION_R = 0.45
RUNNER_BE_TRIGGER_R = 0.75
RUNNER_LOCK_1_TRIGGER_R = 1.35
RUNNER_LOCK_1_R = 0.50
RUNNER_LOCK_2_TRIGGER_R = 2.20
RUNNER_LOCK_2_R = 1.20
RUNNER_LOCK_2_TRAIL_R = 1.00
RUNNER_LOCK_3_TRIGGER_R = 3.20
RUNNER_LOCK_3_R = 2.00
RUNNER_LOCK_3_TRAIL_R = 1.20


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


def _runner_wrapper(base_lock):
    def runner_lock(
        entry_price,
        initial_risk,
        current_sl,
        peak_price,
        current_price,
    ):
        base = dict(base_lock(
            entry_price,
            initial_risk,
            current_sl,
            peak_price,
            current_price,
        ) or {})

        entry = max(TICK_SIZE, _f(entry_price, TICK_SIZE))
        risk = max(TICK_SIZE, _f(initial_risk, TICK_SIZE))
        old_sl = max(TICK_SIZE, _f(current_sl, entry - risk))
        current = max(TICK_SIZE, _f(current_price, TICK_SIZE))
        peak = max(entry, _f(peak_price, entry), current, _f(base.get("peak_price"), entry))
        peak_r = (peak - entry) / risk
        be_price = max(entry, _f(base.get("cost_safe_breakeven_price"), entry))

        new_sl = old_sl
        stage = "RUNNER_INITIAL_ATR"
        locked_r = (new_sl - entry) / risk
        triggered = bool(
            peak_r >= RUNNER_BE_TRIGGER_R
            and peak + 1e-9 >= be_price + TICK_SIZE
        )

        if triggered:
            new_sl = max(new_sl, be_price)
            stage = "RUNNER_TRUE_BE_AFTER_0_75R"
            locked_r = (new_sl - entry) / risk

            if peak_r >= RUNNER_LOCK_1_TRIGGER_R:
                new_sl = max(new_sl, entry + RUNNER_LOCK_1_R * risk)
                stage = "RUNNER_LOCK_0_50R"
                locked_r = (new_sl - entry) / risk

            if peak_r >= RUNNER_LOCK_2_TRIGGER_R:
                new_sl = max(
                    new_sl,
                    entry + RUNNER_LOCK_2_R * risk,
                    peak - RUNNER_LOCK_2_TRAIL_R * risk,
                )
                stage = "RUNNER_TRAIL_AFTER_2_20R"
                locked_r = (new_sl - entry) / risk

            if peak_r >= RUNNER_LOCK_3_TRIGGER_R:
                new_sl = max(
                    new_sl,
                    entry + RUNNER_LOCK_3_R * risk,
                    peak - RUNNER_LOCK_3_TRAIL_R * risk,
                )
                stage = "RUNNER_TRAIL_AFTER_3_20R"
                locked_r = (new_sl - entry) / risk

        peak_room = max(TICK_SIZE, peak - TICK_SIZE)
        candidate = min(new_sl, peak_room)
        if triggered and candidate + 1e-9 < be_price:
            candidate = old_sl
            stage = "RUNNER_WAITING_TRUE_BE_PRICE_ROOM"
            locked_r = (candidate - entry) / risk
            triggered = False

        base.update({
            "sl_price": round(candidate, 2),
            "old_sl_price": round(old_sl, 2),
            "updated": candidate > old_sl + 1e-9,
            "peak_price": round(peak, 2),
            "peak_r": round(peak_r, 2),
            "locked_r": round(locked_r, 2),
            "stage": stage,
            "initial_risk": round(risk, 2),
            "breakeven_triggered": triggered,
            "runner_profit_lock": True,
            "runner_be_trigger_r": RUNNER_BE_TRIGGER_R,
            "runner_lock_schedule": "0.75R_BE_1.35R_LOCK0.5_2.2R_TRAIL_3.2R_TRAIL",
        })
        return base

    runner_lock._okai_expectancy_runner_v1 = True
    return runner_lock


def _install_runner_profit_locks():
    current_backtest = getattr(backtest_routes, "update_option_profit_lock", None)
    if callable(current_backtest) and not getattr(
        current_backtest,
        "_okai_expectancy_runner_v1",
        False,
    ):
        backtest_routes.update_option_profit_lock = _runner_wrapper(current_backtest)

    current_runtime = getattr(angel_fetcher, "_dynamic_profit_lock", None)
    if not callable(current_runtime):
        current_runtime = dynamic_exit.update_option_profit_lock
    if callable(current_runtime) and not getattr(
        current_runtime,
        "_okai_expectancy_runner_v1",
        False,
    ):
        runtime_runner = _runner_wrapper(current_runtime)
        angel_fetcher._dynamic_profit_lock = runtime_runner
        angel_fetcher.update_option_profit_lock = runtime_runner
        dynamic_exit.update_option_profit_lock = runtime_runner
        strategy.update_option_profit_lock = runtime_runner


def _install_runtime_weakness_exit():
    current = runtime._evaluate_exit
    if getattr(current, "_okai_expectancy_weakness_v1", False):
        return

    def expectancy_exit(trade, ltp, market_data, candle_id):
        result = dict(current(trade, ltp, market_data, candle_id) or {})
        if result.get("reason"):
            return result

        entry = max(TICK_SIZE, _f(runtime._v(trade, "entry_price", TICK_SIZE), TICK_SIZE))
        risk = max(TICK_SIZE, _f(result.get("risk"), TICK_SIZE))
        current_r = (_f(ltp, entry) - entry) / risk
        reversal = dict(result.get("reversal") or {})
        no_follow = dict(result.get("no_follow_through") or {})
        opposite_count = _i(reversal.get("opposite_count"), 0)
        bars_held = _i(no_follow.get("bars_held"), 0)
        peak_r = _f(no_follow.get("peak_r"), _f(reversal.get("peak_r"), 0.0))

        fast_weakness = bool(
            current_r <= FAST_WEAKNESS_EXIT_R
            and opposite_count >= 2
        )
        early_no_follow = bool(
            bars_held >= NO_FOLLOW_THROUGH_BARS
            and peak_r < NO_FOLLOW_THROUGH_MAX_PEAK_R
            and opposite_count >= 2
        )

        if fast_weakness:
            result["reason"] = (
                "FAST WEAKNESS EXIT"
                f" | current={current_r:.2f}R"
                f" | weakness={opposite_count}/3"
            )
        elif early_no_follow:
            result["reason"] = (
                "EARLY NO FOLLOW THROUGH EXIT"
                f" | bars={bars_held}"
                f" | peak={peak_r:.2f}R"
                f" | weakness={opposite_count}/3"
            )

        no_follow.update({
            "exit": bool(fast_weakness or early_no_follow),
            "fast_weakness_exit": fast_weakness,
            "early_no_follow_through_exit": early_no_follow,
            "bars_required": NO_FOLLOW_THROUGH_BARS,
            "max_peak_r": NO_FOLLOW_THROUGH_MAX_PEAK_R,
        })
        result["no_follow_through"] = no_follow
        result["expectancy_exit_version"] = "EXPECTANCY_ENGINE_V1"
        return result

    expectancy_exit._okai_expectancy_weakness_v1 = True
    runtime._evaluate_exit = expectancy_exit


def _install_backtest_soft_invalidation():
    current = backtest_cost._risk_capped_trade
    if getattr(current, "_okai_expectancy_soft_stop_v1", False):
        return

    def expectancy_trade(trade, result, broker_name, equity):
        candidate = dict(trade or {})
        entry = max(TICK_SIZE, _f(candidate.get("entry_price"), TICK_SIZE))
        exit_price = max(TICK_SIZE, _f(candidate.get("exit_price"), TICK_SIZE))
        risk = max(TICK_SIZE, _f(candidate.get("risk_points"), entry * 0.08))
        peak_r = _f(candidate.get("peak_r"), 0.0)
        reason = str(candidate.get("reason") or "").upper()
        weak_stop_reason = any(token in reason for token in (
            "PURE_ATR_SL",
            "PURE ATR SL",
            "HARD_PREMIUM_RISK_CAP_SL",
            "HARD_RISK_CAP_SL",
        ))

        soft_exit = round(max(TICK_SIZE, entry - BACKTEST_SOFT_INVALIDATION_R * risk), 2)
        if (
            weak_stop_reason
            and peak_r < NO_FOLLOW_THROUGH_MAX_PEAK_R
            and exit_price + 1e-9 < soft_exit
        ):
            candidate["original_exit_price_before_soft_invalidation"] = round(exit_price, 2)
            candidate["original_reason_before_soft_invalidation"] = candidate.get("reason")
            candidate["exit_price"] = soft_exit
            candidate["reason"] = "EARLY_WEAKNESS_SOFT_STOP_0_45R"
            candidate["expectancy_soft_invalidation"] = True
            candidate["soft_invalidation_r"] = BACKTEST_SOFT_INVALIDATION_R

        output = current(candidate, result, broker_name, equity)
        if isinstance(output, dict):
            output["expectancy_engine_version"] = "EXPECTANCY_ENGINE_V1"
        return output

    expectancy_trade._okai_expectancy_soft_stop_v1 = True
    backtest_cost._risk_capped_trade = expectancy_trade


def apply_expectancy_engine_v1_patch():
    if getattr(runtime, "_okai_expectancy_engine_v1", False):
        return

    _install_runner_profit_locks()
    _install_runtime_weakness_exit()
    _install_backtest_soft_invalidation()

    runtime._okai_expectancy_engine_v1 = True
    backtest_routes._okai_expectancy_engine_v1 = True
