"""Live per-indicator score breakdown for the mobile Trade tab.

This patch keeps the trading decision unchanged. It annotates the active
signal payload and AUTO portfolio scan summaries with the score each enabled
strategy rule is currently contributing. Display scores use partial/proportional
values and the currently active strategy weights.
"""

from __future__ import annotations

from typing import Any

from bot import strategy as strategy_module
from bot import auto_portfolio_runtime as runtime


DIRECTION_KEYS = ("vwap", "supertrend", "ema_trend", "orb", "momentum")

LABELS = {
    "vwap": "VWAP Direction",
    "supertrend": "Supertrend",
    "ema_trend": "EMA9 / EMA21 Trend",
    "orb": "ORB Breakout",
    "momentum": "2-Candle Momentum",
    "adx": "ADX Strength",
    "volume": "Volume Confirmation",
    "availability_normalization": "Data Availability Normalization",
    "accuracy_confirmation": "Accuracy Confirmation",
    "final_score_adjustment": "Final Score Reconciliation",
    "mtf": "Trend / MTF Confirmation",
}

# Fallback must match the permanent OKAI Default 82 / editable default profile.
# Profile-provided weights still win; these values are used only when a scan
# summary does not carry profile_weights yet.
DEFAULT_WEIGHTS = {
    "vwap": 15,
    "supertrend": 13,
    "ema_trend": 18,
    "orb": 15,
    "momentum": 10,
    "adx": 14,
    "volume": 5,
    "mtf": 10,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _b(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _partial(max_score: int, ratio: float) -> int:
    max_score = max(0, _i(max_score, 0))
    if max_score <= 0:
        return 0
    ratio = _clamp(ratio)
    if ratio <= 0:
        return 0
    return max(1, min(max_score, int(round(max_score * ratio))))


def _active_default_weights() -> dict:
    try:
        from bot.default_strategy_patch import TARGET_WEIGHTS

        if isinstance(TARGET_WEIGHTS, dict) and TARGET_WEIGHTS:
            return {key: _i(TARGET_WEIGHTS.get(key), DEFAULT_WEIGHTS[key]) for key in DEFAULT_WEIGHTS}
    except Exception:
        pass
    return dict(DEFAULT_WEIGHTS)


def _weights(profile: dict | None, result: dict | None) -> dict:
    result = result or {}
    profile = profile or {}
    raw = result.get("profile_weights") or profile.get("weights") or {}
    weights = _active_default_weights()
    if isinstance(raw, dict):
        for key, value in raw.items():
            key = str(key)
            if key in DEFAULT_WEIGHTS:
                weights[key] = _i(value, weights.get(key, 0))
    return weights


def _enabled(profile: dict | None, result: dict | None) -> dict:
    result = result or {}
    profile = profile or {}
    raw = result.get("profile_enabled") or profile.get("enabled") or {}
    enabled = {key: True for key in DEFAULT_WEIGHTS}
    if isinstance(raw, dict):
        for key, value in raw.items():
            key = str(key)
            if key in DEFAULT_WEIGHTS:
                enabled[key] = _b(value, True)
    return enabled


def _market_numbers(market: dict) -> dict:
    price = _f(market.get("price"), 0)
    vwap = _f(market.get("vwap"), price)
    ema9 = _f(market.get("ema9"), price)
    ema21 = _f(market.get("ema21"), price)
    atr = max(0.0, _f(market.get("atr"), 0))
    supertrend = str(market.get("supertrend_dir", "NEUTRAL") or "NEUTRAL").upper()
    trend = str(market.get("trend", "SIDEWAYS") or "SIDEWAYS").upper()
    orb_high = _f(market.get("orb_high"), 0)
    orb_low = _f(market.get("orb_low"), 0)
    c1_bull = _b(market.get("c1_bullish"), False)
    c2_bull = _b(market.get("c2_bullish"), False)
    return {
        "price": price,
        "vwap": vwap,
        "ema9": ema9,
        "ema21": ema21,
        "atr": atr,
        "supertrend": supertrend,
        "trend": trend,
        "orb_high": orb_high,
        "orb_low": orb_low,
        "c1_bull": c1_bull,
        "c2_bull": c2_bull,
    }


def _side_checks(market: dict) -> tuple[dict, dict, dict]:
    m = _market_numbers(market)
    price = m["price"]
    vwap = m["vwap"]
    ema9 = m["ema9"]
    ema21 = m["ema21"]
    supertrend = m["supertrend"]
    trend = m["trend"]
    orb_high = m["orb_high"]
    orb_low = m["orb_low"]
    c1_bull = m["c1_bull"]
    c2_bull = m["c2_bull"]

    ce = {
        "vwap": price > vwap,
        "supertrend": supertrend == "UP",
        "ema_trend": ema9 > ema21 and trend == "UPTREND",
        "orb": orb_high > 0 and price > orb_high + 5,
        "momentum": c1_bull and c2_bull,
    }
    pe = {
        "vwap": price < vwap,
        "supertrend": supertrend == "DOWN",
        "ema_trend": ema9 < ema21 and trend == "DOWNTREND",
        "orb": orb_low > 0 and price < orb_low - 5,
        "momentum": (not c1_bull) and (not c2_bull),
    }
    detail = {
        "vwap": f"Price {price:.2f} vs VWAP {vwap:.2f}",
        "supertrend": f"Supertrend {supertrend}",
        "ema_trend": f"EMA9 {ema9:.2f} vs EMA21 {ema21:.2f} | {trend}",
        "orb": f"ORB high {orb_high:.2f} / low {orb_low:.2f}",
        "momentum": f"C1 {'bull' if c1_bull else 'bear'} | C2 {'bull' if c2_bull else 'bear'}",
    }
    return ce, pe, detail


def _direction(ce: bool, pe: bool) -> str:
    if ce and not pe:
        return "CE"
    if pe and not ce:
        return "PE"
    return "WAIT"


def _directional_display_score(key: str, market: dict, candidate: str, max_score: int) -> int:
    """Partial/proportional visual score. Does not change trade decisions."""
    if candidate not in ("CE", "PE"):
        return 0

    m = _market_numbers(market or {})
    price = m["price"]
    atr = max(0.01, m["atr"])
    side_mult = 1 if candidate == "CE" else -1

    if key == "vwap":
        edge = (price - m["vwap"]) * side_mult
        limit = max(8.0, atr * 0.60)
        return _partial(max_score, edge / limit)

    if key == "supertrend":
        st = m["supertrend"]
        if (candidate == "CE" and st == "UP") or (candidate == "PE" and st == "DOWN"):
            return max_score
        if st == "NEUTRAL":
            return _partial(max_score, 0.35)
        return 0

    if key == "ema_trend":
        ema_edge = (m["ema9"] - m["ema21"]) * side_mult
        trend_ok = (candidate == "CE" and m["trend"] == "UPTREND") or (
            candidate == "PE" and m["trend"] == "DOWNTREND"
        )
        limit = max(2.5, atr * 0.20)
        score = _partial(max_score, ema_edge / limit)
        return score if trend_ok else int(round(score * 0.50))

    if key == "orb":
        if m["orb_high"] <= 0 or m["orb_low"] <= 0:
            return 0
        if candidate == "CE":
            edge = price - (m["orb_high"] + 5)
        else:
            edge = (m["orb_low"] - 5) - price
        limit = max(8.0, atr * 0.50)
        return _partial(max_score, edge / limit)

    if key == "momentum":
        desired = bool(candidate == "CE")
        matches = int(m["c1_bull"] == desired) + int(m["c2_bull"] == desired)
        return _partial(max_score, matches / 2.0)

    return 0


def _score_components(market: dict, result: dict, profile: dict | None = None) -> list[dict]:
    profile = profile or {}
    result = result or {}
    weights = _weights(profile, result)
    enabled = _enabled(profile, result)
    candidate = str(result.get("candidate_signal") or result.get("signal") or "WAIT").upper()
    ce, pe, detail = _side_checks(market or {})

    rows: list[dict] = []
    for key in DIRECTION_KEYS:
        max_score = _i(weights.get(key), 0)
        is_enabled = _b(enabled.get(key), True)
        selected_ok = (candidate == "CE" and ce[key]) or (candidate == "PE" and pe[key])
        visual_value = _directional_display_score(key, market or {}, candidate, max_score)
        decision_value = max_score if is_enabled and selected_ok else 0
        rows.append(
            {
                "key": key,
                "label": LABELS[key],
                # score is the exact contribution used by the entry engine.
                # Keep proportional strength separately for diagnostics only.
                "score": decision_value,
                "display_score": decision_value,
                "visual_score": visual_value if is_enabled else 0,
                "decision_score": decision_value,
                "max_score": max_score if is_enabled else 0,
                "enabled": is_enabled,
                "passed": bool(selected_ok and is_enabled),
                "partial": False,
                "visual_partial": bool(
                    is_enabled and 0 < visual_value < max_score
                ),
                "direction": _direction(ce[key], pe[key]),
                "selected_side": candidate,
                "detail": detail[key],
            }
        )

    adx = _f(result.get("adx", (market or {}).get("adx", 0)), 0)
    adx_threshold = _f(
        result.get("adx_threshold", profile.get("adx_threshold", strategy_module.ADX_THRESHOLD)),
        strategy_module.ADX_THRESHOLD,
    )
    adx_enabled = _b(enabled.get("adx"), True)
    adx_weight = _i(weights.get("adx"), 0)
    adx_display = _partial(adx_weight, adx / max(adx_threshold, 1.0)) if adx_enabled else 0
    adx_decision = _i(result.get("adx_bonus"), 0)
    # Some late runtime wrappers preserve the normalized final score but leave
    # an older zero bonus in the display snapshot. Rebuild that stale field from
    # the same active threshold/weight used by the strategy.
    if adx_enabled and adx >= adx_threshold and adx_decision <= 0:
        is_protected_default = str(
            result.get("strategy_profile_key")
            or profile.get("profile_key")
            or "okai_default_82"
        ) == "okai_default_82"
        adx_decision = (
            min(
                adx_weight,
                max(0, int((adx - adx_threshold) * 0.8 + 10)),
            )
            if is_protected_default
            else adx_weight
        )
    if not adx_enabled:
        adx_decision = 0
    rows.append(
        {
            "key": "adx",
            "label": LABELS["adx"],
            "score": adx_decision,
            "display_score": adx_decision,
            "visual_score": adx_display,
            "decision_score": adx_decision,
            "max_score": adx_weight if adx_enabled else 0,
            "enabled": adx_enabled,
            "passed": bool(adx_enabled and adx >= adx_threshold),
            "partial": False,
            "visual_partial": bool(
                adx_enabled and 0 < adx_display < adx_weight
            ),
            "direction": "STRENGTH",
            "selected_side": candidate,
            "detail": f"ADX {adx:.1f} / threshold {adx_threshold:.1f}",
        }
    )

    volume_ratio = _f(result.get("volume_ratio", (market or {}).get("volume_ratio", 0)), 0)
    volume_threshold = _f(
        result.get("volume_threshold", profile.get("volume_threshold", strategy_module.VOLUME_RATIO_THRESHOLD)),
        strategy_module.VOLUME_RATIO_THRESHOLD,
    )
    volume_available = _b(
        result.get("volume_available", (market or {}).get("volume_available", volume_ratio > 0)),
        volume_ratio > 0,
    )
    volume_enabled = _b(enabled.get("volume"), True)
    volume_weight = _i(weights.get("volume"), 0)
    availability_normalized = _b(
        result.get("availability_normalized"),
        False,
    )
    effective_volume_weight = volume_weight
    if availability_normalized and not volume_available:
        effective_volume_weight = min(
            volume_weight,
            max(
                0,
                _i(
                    result.get("volume_bonus"),
                    getattr(strategy_module, "VOLUME_NEUTRAL_BONUS", 7),
                ),
            ),
        )
    if not volume_enabled:
        volume_display = 0
    elif not volume_available:
        volume_display = (
            effective_volume_weight
            if availability_normalized
            else _partial(volume_weight, 0.50)
        )
    else:
        volume_display = _partial(volume_weight, volume_ratio / max(volume_threshold, 0.01))
    volume_decision = _i(result.get("volume_bonus"), 0)
    if volume_enabled and not volume_available and volume_decision <= 0:
        is_protected_default = str(
            result.get("strategy_profile_key")
            or profile.get("profile_key")
            or "okai_default_82"
        ) == "okai_default_82"
        volume_decision = min(
            volume_weight,
            (
                getattr(strategy_module, "VOLUME_NEUTRAL_BONUS", 7)
                if is_protected_default
                else _partial(volume_weight, 0.50)
            ),
        )
    rows.append(
        {
            "key": "volume",
            "label": LABELS["volume"],
            "score": volume_decision if volume_enabled else 0,
            "display_score": volume_decision if volume_enabled else 0,
            "visual_score": volume_display,
            "decision_score": volume_decision if volume_enabled else 0,
            "max_score": effective_volume_weight if volume_enabled else 0,
            "enabled": volume_enabled,
            "passed": bool(volume_enabled and (not volume_available or volume_ratio >= volume_threshold)),
            "partial": False,
            "visual_partial": bool(
                volume_enabled and 0 < volume_display < effective_volume_weight
            ),
            "direction": "CONFIRM",
            "selected_side": candidate,
            "detail": (
                (
                    "Index volume unavailable: neutral 7-point contribution"
                    if availability_normalized
                    else "Volume unavailable: 50% neutral display score"
                )
                if not volume_available
                else f"Volume {volume_ratio:.2f}x / threshold {volume_threshold:.2f}x"
            ),
            "preserve_backend_scale": bool(
                availability_normalized and not volume_available
            ),
        }
    )

    if availability_normalized:
        configured_score_max = _i(
            result.get("configured_score_max"),
            getattr(strategy_module, "TQU_SCORE_MAX", 100),
        )
        effective_score_max = _i(
            result.get("effective_score_max"),
            configured_score_max,
        )
        adjustment_max = max(
            0,
            configured_score_max - effective_score_max,
        )
        adjustment = min(
            adjustment_max,
            max(0, _i(result.get("availability_adjustment"), 0)),
        )
        pre_normalization_score = _i(
            result.get("pre_normalization_score"),
            _i(result.get("score"), 0) - adjustment,
        )
        normalized_score = _i(
            result.get("score"),
            pre_normalization_score + adjustment,
        )
        rows.append(
            {
                "key": "availability_normalization",
                "label": LABELS["availability_normalization"],
                "score": adjustment,
                "display_score": adjustment,
                "visual_score": adjustment,
                "decision_score": adjustment,
                "max_score": adjustment_max,
                "enabled": True,
                "passed": adjustment > 0,
                "partial": bool(0 < adjustment < adjustment_max),
                "direction": "NORMALIZE",
                "selected_side": candidate,
                "detail": (
                    "Index volume unavailable; quality score normalized "
                    f"{pre_normalization_score}/{effective_score_max} to "
                    f"{normalized_score}/{configured_score_max}"
                ),
                "preserve_backend_scale": True,
            }
        )

    mtf_enabled = _b(enabled.get("mtf"), True)
    mtf_ok = _b(result.get("mtf_confirmed", (market or {}).get("mtf_confirmed", False)), False)
    mtf_weight = _i(weights.get("mtf"), 0)
    mtf_display = mtf_weight if mtf_enabled and mtf_ok else 0
    mtf_decision = _i(result.get("mtf_bonus"), 0)
    if mtf_enabled and mtf_ok and mtf_decision <= 0:
        mtf_decision = mtf_display
    rows.append(
        {
            "key": "mtf",
            "label": LABELS["mtf"],
            "score": mtf_decision if mtf_enabled else 0,
            "display_score": mtf_decision if mtf_enabled else 0,
            "visual_score": mtf_display,
            "decision_score": mtf_decision if mtf_enabled else 0,
            "max_score": mtf_weight if mtf_enabled else 0,
            "enabled": mtf_enabled,
            "passed": bool(mtf_enabled and mtf_ok),
            "partial": False,
            "direction": "CONFIRM",
            "selected_side": candidate,
            "detail": "MTF confirmed" if mtf_ok else "MTF not confirmed",
        }
    )

    return rows


def _score_payload(market: dict, result: dict, profile: dict | None = None) -> dict:
    profile = profile or {}
    result = result or {}
    weights = _weights(profile, result)
    enabled = _enabled(profile, result)
    components = _score_components(market or {}, result, {"weights": weights, "enabled": enabled, **profile})
    accuracy_adjustment = _i(result.get("accuracy_adjustment"), 0)
    if accuracy_adjustment:
        components.append(
            {
                "key": "accuracy_confirmation",
                "label": LABELS["accuracy_confirmation"],
                "score": accuracy_adjustment,
                "display_score": accuracy_adjustment,
                "visual_score": accuracy_adjustment,
                "decision_score": accuracy_adjustment,
                "max_score": 7,
                "enabled": True,
                "passed": accuracy_adjustment > 0,
                "partial": abs(accuracy_adjustment) < 7,
                "direction": "ADJUST",
                "selected_side": str(
                    result.get("candidate_signal")
                    or result.get("signal")
                    or "WAIT"
                ),
                "detail": f"Accuracy confirmation {accuracy_adjustment:+d}",
            }
        )

    component_total = sum(_i(item.get("score"), 0) for item in components)
    visual_total = sum(
        _i(item.get("visual_score", item.get("score")), 0)
        for item in components
    )
    decision_component_total = sum(_i(item.get("decision_score"), 0) for item in components)
    raw_total = sum(
        _i(item.get("max_score"), 0)
        for item in components
        if _b(item.get("enabled"), True)
        and str(item.get("key") or "") not in {
            "accuracy_confirmation",
            "final_score_adjustment",
        }
    )
    final_score = _i(result.get("score"), decision_component_total)
    # The final score is authoritative. If a legacy wrapper changed/capped it
    # without updating component rows, expose the exact residual instead of
    # showing an impossible header-versus-breakdown mismatch.
    reconciliation = final_score - decision_component_total
    if reconciliation:
        components.append(
            {
                "key": "final_score_adjustment",
                "label": LABELS["final_score_adjustment"],
                "score": reconciliation,
                "display_score": reconciliation,
                "visual_score": reconciliation,
                "decision_score": reconciliation,
                "max_score": abs(reconciliation),
                "enabled": True,
                "passed": reconciliation >= 0,
                "partial": False,
                "direction": "RECONCILE",
                "selected_side": str(
                    result.get("candidate_signal")
                    or result.get("signal")
                    or "WAIT"
                ),
                "detail": (
                    "Final decision score reconciliation "
                    f"{reconciliation:+d}"
                ),
            }
        )
        component_total += reconciliation
        decision_component_total += reconciliation
        visual_total += reconciliation
    min_score = _i(result.get("min_score", result.get("min_score_required", 82)), 82)
    return {
        "score": final_score,
        "display_score": final_score,
        "decision_score": final_score,
        "component_total": component_total,
        "decision_component_total": decision_component_total,
        # Public score fields stay canonical. The proportional diagnostic is
        # explicitly separated so no screen can mistake it for entry quality.
        "visual_strength_score": final_score,
        "diagnostic_visual_strength_score": visual_total,
        "component_score_matches_decision": bool(
            component_total == final_score
            and decision_component_total == final_score
        ),
        "enabled_weight_total": raw_total,
        "min_score": min_score,
        "candidate_signal": str(result.get("candidate_signal") or result.get("signal") or "WAIT"),
        "trade_allowed": _b(result.get("trade_allowed"), False),
        "components": components,
        "warnings": list(result.get("warnings", []) or [])[:8],
        "score_mode": "CANONICAL_DECISION_COMPONENTS_V1",
        "availability_normalized": _b(
            result.get("availability_normalized"),
            False,
        ),
        "availability_adjustment": _i(
            result.get("availability_adjustment"),
            0,
        ),
        "pre_normalization_score": _i(
            result.get("pre_normalization_score"),
            final_score,
        ),
        "effective_score_max": _i(
            result.get("effective_score_max"),
            getattr(strategy_module, "TQU_SCORE_MAX", 100),
        ),
        "configured_score_max": _i(
            result.get("configured_score_max"),
            getattr(strategy_module, "TQU_SCORE_MAX", 100),
        ),
        "availability_score_mode": str(
            result.get("availability_score_mode") or "STANDARD"
        ),
        "profile_weights": weights,
        "profile_enabled": enabled,
    }


def apply_live_score_breakdown_patch() -> None:
    if getattr(strategy_module, "_okai_live_score_breakdown_v3", False):
        return

    original_get_full_signal = strategy_module.get_full_signal
    original_summary = runtime._summary

    def get_full_signal_with_breakdown(market_data, consecutive_losses=0, profile=None):
        result = original_get_full_signal(
            market_data,
            consecutive_losses=consecutive_losses,
            profile=profile,
        )
        if not isinstance(result, dict):
            return result

        profile = profile or {}
        result.setdefault("adx_threshold", _f(profile.get("adx_threshold"), strategy_module.ADX_THRESHOLD))
        result.setdefault("volume_threshold", _f(profile.get("volume_threshold"), strategy_module.VOLUME_RATIO_THRESHOLD))
        result.setdefault("profile_weights", _weights(profile, result))
        result.setdefault("profile_enabled", _enabled(profile, result))
        payload = _score_payload(market_data or {}, result, profile)
        result["score_components"] = payload["components"]
        result["live_score_breakdown"] = payload
        return result

    def summary_with_breakdown(scan):
        data = original_summary(scan)
        if not isinstance(data, dict) or not isinstance(scan, dict):
            return data
        signal = scan.get("signal_data") or {}
        market = scan.get("market_data") or {}
        effective_weights = _weights(None, signal)
        effective_enabled = _enabled(None, signal)
        profile_like = {
            "weights": effective_weights,
            "enabled": effective_enabled,
            "adx_threshold": signal.get("adx_threshold", strategy_module.ADX_THRESHOLD),
            "volume_threshold": signal.get("volume_threshold", strategy_module.VOLUME_RATIO_THRESHOLD),
        }
        payload = signal.get("live_score_breakdown") or _score_payload(market, signal, profile_like)
        # Keep the original engine value as decision_score. Set score to the display
        # score so the mobile live score card and mini rows show the same partial
        # active-profile total that users see in the breakdown.
        engine_score = data.get("score")
        data.update(
            {
                "score": payload.get("display_score", payload.get("score", engine_score)),
                "score_components": payload.get("components", []),
                "live_score_breakdown": payload,
                "display_score": payload.get("display_score"),
                "decision_score": payload.get("decision_score", engine_score),
                "component_total": payload.get("component_total"),
                "decision_component_total": payload.get("decision_component_total"),
                "enabled_weight_total": payload.get("enabled_weight_total"),
                "adx_threshold": profile_like["adx_threshold"],
                "volume_threshold": profile_like["volume_threshold"],
                "profile_weights": payload.get("profile_weights", effective_weights),
                "profile_enabled": payload.get("profile_enabled", effective_enabled),
                "score_mode": payload.get("score_mode"),
            }
        )
        return data

    strategy_module.get_full_signal = get_full_signal_with_breakdown
    runtime._summary = summary_with_breakdown

    try:
        import bot.angel_fetcher as angel_fetcher

        angel_fetcher.get_full_signal = get_full_signal_with_breakdown
    except Exception:
        pass

    strategy_module._okai_live_score_breakdown_v3 = True
    strategy_module._okai_live_score_breakdown_v2 = True
    strategy_module._okai_live_score_breakdown_v1 = True
