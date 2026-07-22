"""Calibrated entry-timing gate for Default 82.

Score is indicator agreement, not probability. A mature trend can stay 100/82
while price is already too far from EMA/VWAP and the option premium is near the
end of its move. The June regression appeared after the older fresh-distance
gate was removed, allowing repeated late 100-score entries.

This final pipeline guard restores the previously profitable timing limits:
- EMA distance <= 0.95 spot ATR
- VWAP distance <= 2.20 spot ATR when real VWAP is available

The shared point/ATR anti-chase, mandatory structure, fresh trigger, reversal,
ORB exhaustion and option-premium guards remain active. Score 82 is unchanged.
"""

from bot import angel_fetcher
from bot import routes
from bot import strategy


EMA_MAX_ATR = 0.95
VWAP_MAX_ATR = 2.20
EMA_REASON = "EMA_EXTENSION_OVER_0.95_ATR"
VWAP_REASON = "VWAP_EXTENSION_OVER_2.20_ATR"


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _append_unique(values, value):
    output = list(values or [])
    if value not in output:
        output.append(value)
    return output


def _apply_timing_gate(result, market_data):
    if not isinstance(result, dict):
        return result

    output = dict(result)
    candidate = str(
        output.get("candidate_signal") or output.get("signal") or "WAIT"
    ).upper()
    score = int(round(_f(output.get("score"), 0)))
    minimum = int(round(_f(output.get("min_score", 82), 82)))

    price = _f((market_data or {}).get("price"), 0)
    ema9 = _f((market_data or {}).get("ema9"), price)
    vwap = _f((market_data or {}).get("vwap"), price)
    atr = max(0.01, _f((market_data or {}).get("atr"), 0.01))
    vwap_fallback = bool((market_data or {}).get("vwap_fallback_used", False))

    ema_distance_atr = abs(price - ema9) / atr
    vwap_distance_atr = abs(price - vwap) / atr

    timing_reasons = []
    if candidate in ("CE", "PE") and score >= minimum:
        if ema_distance_atr > EMA_MAX_ATR:
            timing_reasons.append(EMA_REASON)
        if not vwap_fallback and vwap_distance_atr > VWAP_MAX_ATR:
            timing_reasons.append(VWAP_REASON)

    safety_reasons = list(output.get("safety_gate_reasons") or [])
    fresh_reasons = list(output.get("fresh_entry_block_reasons") or [])
    warnings = list(output.get("warnings") or [])

    for reason in timing_reasons:
        safety_reasons = _append_unique(safety_reasons, reason)
        fresh_reasons = _append_unique(fresh_reasons, reason)
        warnings = _append_unique(warnings, "ENTRY_TIMING_BLOCK:" + reason)

    blocked = bool(timing_reasons)
    previously_allowed = bool(output.get("trade_allowed", False))
    eligible = bool(previously_allowed and not blocked)

    output.update({
        "signal": candidate if eligible else "WAIT",
        "candidate_signal": candidate,
        "trade_allowed": eligible,
        "safety_gate_passed": eligible,
        "fresh_entry_ok": not fresh_reasons,
        "safety_gate_reasons": safety_reasons,
        "fresh_entry_block_reasons": fresh_reasons,
        "warnings": warnings,
        "ema_distance_atr": round(ema_distance_atr, 2),
        "vwap_distance_atr": round(vwap_distance_atr, 2),
        "entry_timing_blocked": blocked,
        "entry_timing_block_reasons": timing_reasons,
        "entry_timing_calibration": {
            "ema_max_atr": EMA_MAX_ATR,
            "vwap_max_atr": VWAP_MAX_ATR,
        },
        "entry_timing_mode": "CALIBRATED_FRESH_DISTANCE_V1",
    })
    return output


def apply_entry_timing_calibration_patch():
    if getattr(strategy, "_okai_entry_timing_calibration_v1", False):
        return

    original = strategy.get_full_signal

    def calibrated_signal(market_data, consecutive_losses=0, profile=None):
        result = original(
            market_data,
            consecutive_losses=consecutive_losses,
            profile=profile,
        )
        return _apply_timing_gate(result, market_data or {})

    strategy.get_full_signal = calibrated_signal
    routes.get_full_signal = calibrated_signal
    angel_fetcher.get_full_signal = calibrated_signal
    strategy._okai_entry_timing_calibration_v1 = True
