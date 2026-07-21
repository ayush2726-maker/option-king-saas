"""Fresh Entry Guard V1.

Why this exists:
- Index feeds (especially Upstox index candles) often publish zero volume.
- The strategy still reserved 15 score points for volume, so a good setup stayed
  near 74 and crossed 82 only after the second same-direction candle.
- That made entries late, often near a local peak/bottom.

This patch keeps the protected entry threshold at 82, treats genuinely missing
index volume as unavailable (not weak), normalises the remaining score, and
blocks exhausted/reversing entries using ATR-scaled freshness checks.
"""

import math

from bot import strategy
from bot import angel_fetcher
from bot import routes


def _f(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except Exception:
        return float(default)


def _b(value):
    return bool(value)


def _directional_state(market_data):
    price = _f(market_data.get("price"))
    vwap = _f(market_data.get("vwap"), price)
    ema9 = _f(market_data.get("ema9"), price)
    ema21 = _f(market_data.get("ema21"), price)
    supertrend = str(market_data.get("supertrend_dir") or "NEUTRAL").upper()
    trend = str(market_data.get("trend") or "SIDEWAYS").upper()
    orb_high = _f(market_data.get("orb_high"))
    orb_low = _f(market_data.get("orb_low"))
    c1_bull = _b(market_data.get("c1_bullish"))
    c2_bull = _b(market_data.get("c2_bullish"))

    ce_checks = {
        "vwap": price > vwap,
        "supertrend": supertrend == "UP",
        "ema_trend": ema9 > ema21 and trend == "UPTREND",
        "orb": orb_high > 0 and price > orb_high + 5,
        "momentum": c1_bull and c2_bull,
    }
    pe_checks = {
        "vwap": price < vwap,
        "supertrend": supertrend == "DOWN",
        "ema_trend": ema9 < ema21 and trend == "DOWNTREND",
        "orb": orb_low > 0 and price < orb_low - 5,
        "momentum": (not c1_bull) and (not c2_bull),
    }

    ce_count = sum(1 for value in ce_checks.values() if value)
    pe_count = sum(1 for value in pe_checks.values() if value)
    candidate = "CE" if ce_count > pe_count else "PE" if pe_count > ce_count else "WAIT"

    return {
        "candidate": candidate,
        "ce_count": ce_count,
        "pe_count": pe_count,
        "price": price,
        "vwap": vwap,
        "ema9": ema9,
        "ema21": ema21,
        "orb_high": orb_high,
        "orb_low": orb_low,
        "c1_bull": c1_bull,
        "c2_bull": c2_bull,
    }


def _volume_weight(profile):
    if isinstance(profile, dict) and str(profile.get("profile_key") or "") != "okai_default_82":
        enabled = dict(profile.get("enabled") or {})
        if not enabled.get("volume", True):
            return 0
        try:
            return max(0, min(40, int((profile.get("weights") or {}).get("volume", 15))))
        except Exception:
            return 15
    return 15


def _apply_fresh_entry_guard(result, market_data, profile):
    if not isinstance(result, dict):
        return result

    output = dict(result)
    warnings = list(output.get("warnings") or [])
    state = _directional_state(market_data)

    candidate = str(output.get("candidate_signal") or "").upper()
    if candidate not in ("CE", "PE"):
        candidate = state["candidate"]

    raw_score = int(round(_f(output.get("score"), 0)))
    required = int(round(_f(
        output.get("min_score", output.get("min_score_required", strategy.WEIGHTED_MIN_ENTRY_SCORE)),
        strategy.WEIGHTED_MIN_ENTRY_SCORE,
    )))

    volume_ratio = _f(market_data.get("volume_ratio"), 0)
    vwap_fallback = bool(market_data.get("vwap_fallback_used", False))
    volume_weight = _volume_weight(profile)
    volume_unavailable = volume_weight > 0 and volume_ratio <= 0 and vwap_fallback

    adjusted_score = raw_score
    if volume_unavailable:
        available_max = max(50, 100 - volume_weight)
        adjusted_score = min(100, int(round(raw_score * 100.0 / available_max)))
        if adjusted_score != raw_score:
            warnings.append(
                f"INDEX_VOLUME_UNAVAILABLE_NORMALIZED:{raw_score}->{adjusted_score}"
            )

    core_count = state["ce_count"] if candidate == "CE" else state["pe_count"] if candidate == "PE" else 0
    atr = max(0.01, _f(market_data.get("atr"), 0.01))
    price = state["price"]

    ema_distance_atr = abs(price - state["ema9"]) / atr
    vwap_distance_atr = abs(price - state["vwap"]) / atr
    if candidate == "CE" and state["orb_high"] > 0:
        orb_extension_atr = max(0.0, price - (state["orb_high"] + 5)) / atr
        current_aligned = state["c2_bull"]
        two_candle_run = state["c1_bull"] and state["c2_bull"]
    elif candidate == "PE" and state["orb_low"] > 0:
        orb_extension_atr = max(0.0, (state["orb_low"] - 5) - price) / atr
        current_aligned = not state["c2_bull"]
        two_candle_run = (not state["c1_bull"]) and (not state["c2_bull"])
    else:
        orb_extension_atr = 0.0
        current_aligned = False
        two_candle_run = False

    fresh_block_reasons = []
    if candidate in ("CE", "PE") and adjusted_score >= required:
        if core_count < 4:
            fresh_block_reasons.append("CORE_CONFIRMATIONS_BELOW_4")
        if not current_aligned:
            fresh_block_reasons.append("REVERSAL_CANDLE_AT_ENTRY")
        if ema_distance_atr > 0.95:
            fresh_block_reasons.append("EMA_EXTENSION_OVER_0.95_ATR")
        if orb_extension_atr > 1.35:
            fresh_block_reasons.append("ORB_EXTENSION_OVER_1.35_ATR")
        if (not vwap_fallback) and vwap_distance_atr > 2.20:
            fresh_block_reasons.append("VWAP_EXTENSION_OVER_2.20_ATR")
        if two_candle_run and ema_distance_atr > 0.80 and orb_extension_atr > 0.90:
            fresh_block_reasons.append("LATE_TWO_CANDLE_EXHAUSTION")

    existing_blocked = bool(
        output.get("chase_blocked")
        or output.get("anti_chase_blocked")
        or output.get("ema_chase_blocked")
        or output.get("vwap_chase_blocked")
        or output.get("sideways_blocked")
    )

    fresh_ok = not fresh_block_reasons
    trade_allowed = bool(
        candidate in ("CE", "PE")
        and adjusted_score >= required
        and core_count >= 4
        and fresh_ok
        and not existing_blocked
    )

    if fresh_block_reasons:
        warnings.extend("FRESH_ENTRY_BLOCK:" + reason for reason in fresh_block_reasons)

    output.update({
        "signal": candidate if trade_allowed else "WAIT",
        "candidate_signal": candidate,
        "score_before_volume_normalize": raw_score,
        "score": adjusted_score,
        "trade_allowed": trade_allowed,
        "min_score": required,
        "volume_data_available": not volume_unavailable,
        "volume_score_normalized": volume_unavailable,
        "fresh_entry_ok": fresh_ok,
        "fresh_entry_block_reasons": fresh_block_reasons,
        "core_confirmations": core_count,
        "ema_distance_atr": round(ema_distance_atr, 2),
        "vwap_distance_atr": round(vwap_distance_atr, 2),
        "orb_extension_atr": round(orb_extension_atr, 2),
        "entry_timing_mode": "FRESH_ATR_GUARD_V1",
        "warnings": warnings,
    })
    return output


def _premium_entry_quality_v2(rows, current_ltp):
    candles = angel_fetcher._normalize_option_candles(rows)
    current = _f(current_ltp, 0)

    if current <= 0:
        return {"allowed": False, "reason": "INVALID_OPTION_LTP", "current_ltp": current}

    if len(candles) < 4:
        return {
            "allowed": True,
            "reason": "OPTION_CANDLES_INSUFFICIENT_FRESH_SPOT_GUARD_ACTIVE",
            "current_ltp": round(current, 2),
            "candle_count": len(candles),
        }

    recent = candles[-6:]
    closes = [candle["close"] for candle in recent]
    highs = [candle["high"] for candle in recent]
    oldest_close = max(0.05, closes[0])
    recent_high = max(highs)
    sorted_closes = sorted(closes)
    middle = len(sorted_closes) // 2
    median_close = (
        sorted_closes[middle]
        if len(sorted_closes) % 2
        else (sorted_closes[middle - 1] + sorted_closes[middle]) / 2
    )

    latest = recent[-1]
    rise_pct = (current / oldest_close - 1) * 100
    pullback_pct = (current / recent_high - 1) * 100
    extension_pct = (current / max(0.05, median_close) - 1) * 100
    latest_bearish = latest["close"] < latest["open"]
    latest_range = max(0.05, latest["high"] - latest["low"])
    upper_wick_ratio = max(0.0, latest["high"] - max(latest["open"], latest["close"])) / latest_range

    spike_reversal = rise_pct >= 8.0 and pullback_pct <= -2.5
    bearish_after_run = rise_pct >= 6.0 and latest_bearish
    rejection_after_run = rise_pct >= 5.0 and latest_bearish and upper_wick_ratio >= 0.30
    extreme_extension = extension_pct >= 10.0
    blocked = spike_reversal or bearish_after_run or rejection_after_run or extreme_extension

    if spike_reversal:
        reason = "OPTION_SPIKE_REVERSING_V2"
    elif rejection_after_run:
        reason = "OPTION_UPPER_WICK_REJECTION_V2"
    elif bearish_after_run:
        reason = "OPTION_BEARISH_AFTER_RUN_V2"
    elif extreme_extension:
        reason = "OPTION_PREMIUM_OVEREXTENDED_V2"
    else:
        reason = "OPTION_PREMIUM_ENTRY_OK_V2"

    return {
        "allowed": not blocked,
        "reason": reason,
        "current_ltp": round(current, 2),
        "candle_count": len(candles),
        "rise_pct": round(rise_pct, 2),
        "pullback_pct": round(pullback_pct, 2),
        "extension_pct": round(extension_pct, 2),
        "upper_wick_ratio": round(upper_wick_ratio, 2),
        "latest_bearish": bool(latest_bearish),
    }


def apply_fresh_entry_guard_patch():
    if getattr(strategy, "_okai_fresh_entry_guard_v1", False):
        return

    original_get_full_signal = strategy.get_full_signal

    def guarded_get_full_signal(market_data, consecutive_losses=0, profile=None):
        result = original_get_full_signal(
            market_data,
            consecutive_losses=consecutive_losses,
            profile=profile,
        )
        return _apply_fresh_entry_guard(result, market_data, profile)

    strategy.get_full_signal = guarded_get_full_signal
    routes.get_full_signal = guarded_get_full_signal
    angel_fetcher.get_full_signal = guarded_get_full_signal
    angel_fetcher._premium_entry_quality = _premium_entry_quality_v2
    strategy._okai_fresh_entry_guard_v1 = True
