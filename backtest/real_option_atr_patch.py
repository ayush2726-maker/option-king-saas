"""Use causal ATR14 from the selected option's own one-minute OHLC candles.

The compiled backtest calls the entry resolver immediately before
calculate_option_atr_levels. A thread-local bridge carries the pre-entry option
ATR into that call, keeping concurrent range jobs isolated and avoiding any
future-candle leakage.
"""

from __future__ import annotations

import threading

from backtest import routes


TICK_SIZE = 0.05
MAX_PREMIUM_RISK_PERCENT = 8.0
_CONTEXT = threading.local()


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _option_atr_before_entry(result, periods=14):
    bars = result.get("bars") or {}
    keys = list(result.get("bar_keys") or [])
    entry_minute = int(result.get("entry_minute") or 0)
    prior_keys = [key for key in keys if key < entry_minute]
    if len(prior_keys) < 2:
        return 0.0, len(prior_keys)

    selected = prior_keys[-(periods + 1):]
    true_ranges = []
    previous_close = None
    for key in selected:
        bar = bars.get(key) or {}
        high = _f(bar.get("high"), 0)
        low = _f(bar.get("low"), 0)
        close = _f(bar.get("close"), 0)
        if high <= 0 or low <= 0 or close <= 0:
            continue
        if previous_close is None:
            true_range = high - low
        else:
            true_range = max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        true_ranges.append(max(0.0, true_range))
        previous_close = close

    if not true_ranges:
        return 0.0, 0
    values = true_ranges[-periods:]
    return sum(values) / len(values), len(values)


def apply_real_option_atr_patch():
    if getattr(routes, "_okai_real_option_atr_v1", False):
        return

    original_prepare = routes._okai_real_premium_prepare_entry
    original_levels = routes.calculate_option_atr_levels

    def prepare_with_option_atr(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        if isinstance(result, dict) and result.get("success"):
            option_atr, samples = _option_atr_before_entry(result)
            result["real_option_atr14"] = round(option_atr, 4)
            result["real_option_atr_samples"] = samples
            _CONTEXT.value = {
                "option_atr": option_atr,
                "samples": samples,
                "premium_model": result.get("premium_source"),
            }
        else:
            try:
                delattr(_CONTEXT, "value")
            except AttributeError:
                pass
        return result

    def real_option_atr_levels(
        spot_price,
        option_entry_price,
        spot_atr,
        is_expiry_day=False,
        sl_floor_percent=0.0,
        reward_multiple=0.0,
    ):
        context = getattr(_CONTEXT, "value", None)
        try:
            delattr(_CONTEXT, "value")
        except AttributeError:
            pass

        if not context or _f(context.get("option_atr"), 0) <= 0:
            # Insufficient pre-entry option candles are rare early in the day.
            # Keep the existing hard-capped ATR formula, but mark it explicitly;
            # the premium path itself remains fully real.
            output = original_levels(
                spot_price,
                option_entry_price,
                spot_atr,
                is_expiry_day=is_expiry_day,
                sl_floor_percent=sl_floor_percent,
                reward_multiple=reward_multiple,
            )
            output = dict(output)
            output["mode"] = "REAL_PREMIUM_SPOT_ATR_FALLBACK"
            output["real_option_atr_available"] = False
            output["real_option_atr_samples"] = int(
                (context or {}).get("samples") or 0
            )
            return output

        entry = max(TICK_SIZE, _f(option_entry_price, TICK_SIZE))
        option_atr = max(TICK_SIZE, _f(context.get("option_atr"), TICK_SIZE))
        multiplier = 1.5 if is_expiry_day else 1.2
        raw_risk = option_atr * multiplier
        premium_cap = entry * MAX_PREMIUM_RISK_PERCENT / 100.0
        risk = min(
            max(TICK_SIZE, raw_risk),
            max(TICK_SIZE, premium_cap),
            max(TICK_SIZE, entry - TICK_SIZE),
        )
        return {
            "mode": (
                "REAL_OPTION_ATR14_EXPIRY"
                if is_expiry_day
                else "REAL_OPTION_ATR14_NORMAL"
            ),
            "spot_price": round(_f(spot_price), 2),
            "spot_atr": round(_f(spot_atr), 2),
            "atr_available": True,
            "response_factor": None,
            "estimated_option_atr": round(option_atr, 2),
            "real_option_atr14": round(option_atr, 4),
            "real_option_atr_available": True,
            "real_option_atr_samples": int(context.get("samples") or 0),
            "atr_multiplier": multiplier,
            "atr_risk_points": round(raw_risk, 2),
            "percentage_risk_points": round(premium_cap, 2),
            "sl_floor_percent": 0.0,
            "risk_points": round(risk, 2),
            "sl_price": round(max(TICK_SIZE, entry - risk), 2),
            "target_price": None,
            "reward_multiple": None,
            "is_expiry_day": bool(is_expiry_day),
            "fixed_target_enabled": False,
            "hard_premium_risk_cap_percent": MAX_PREMIUM_RISK_PERCENT,
            "hard_risk_cap_applied": bool(risk + 1e-9 < raw_risk),
            "quantity_preserved": True,
            "premium_path": "REAL_OPTION_OHLC_1M",
        }

    routes._okai_real_premium_prepare_entry = prepare_with_option_atr
    routes.calculate_option_atr_levels = real_option_atr_levels
    routes._okai_real_option_atr_v1 = True
