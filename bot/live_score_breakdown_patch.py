"""Live per-indicator score breakdown for the mobile Trade tab.

This patch keeps the trading decision unchanged. It only annotates the active
signal payload and AUTO portfolio scan summaries with the score each enabled
strategy rule is currently contributing. The displayed component scores are
partial/proportional, so the UI does not show only 0 or full weight.
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
    "mtf": "Trend / MTF Confirmation",
}

DEFAULT_WEIGHTS = {
    "vwap": 11,
    "supertrend": 11,
    "ema_trend": 11,
    "orb": 11,
    "momentum": 11,
    "adx": 20,
    "volume": 15,
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


def _weights(profile: dict | None, result: dict | None) -> dict:
    result = result or {}
    profile = profile or {}
    raw = result.get("profile_weights") or profile.get("weights") or {}
    weights = dict(DEFAULT_WEIGHTS)
    if isinstance(raw, dict):
        for key, value in raw.items():
            weights[str(key)] = _i(value, weights.get(str(key), 0))
    return weights


def _enabled(profile: dict | None, result: dict | None) -> dict:
    result = result or {}
    profile = profile or {}
    raw = result.get("profile_enabled") or profile.get("enabled") or {}
    enabled = {key: True for key in DEFAULT_WEIGHTS}
    if isinstance(raw, dict):
        for key, value in raw.items():
            enabled[str(key)] = _b(value, True)
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
        value = visual_value if is_enabled else 0
        rows.append(
            {
                "key": key,
                "label": LABELS[key],
                "score": value,
                "decision_score": max_score if is_enabled and selected_ok else 0,
                "max_score": max_score if is_enabled else 0,
                "enabled": is_enabled,
                "passed": bool(selected_ok and is_enabled),
                "partial": bool(is_enabled and 0 < value < max_score),
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
    adx_decision = adx_weight if adx_enabled and adx >= adx_threshold else 0
    rows.append(
        {
            "key": "adx",
            "label": LABELS["adx"],
            "score": adx_display,
            "decision_score": adx_decision,
            "max_score": adx_weight if adx_enabled else 0,
            "enabled": adx_enabled,
            "passed": bool(adx_enabled and adx >= adx_threshold),
            "partial": bool(adx_enabled and 0 < adx_display < adx_weight),
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
    if not volume_enabled:
        volume_display = 0
    elif not volume_available:
        volume_display = _partial(volume_weight, 0.50)
    else:
        volume_display = _partial(volume_weight, volume_ratio / max(volume_threshold, 0.01))
    volume_decision = _i(result.get("volume_bonus"), 0)
    if volume_enabled and not volume_available and volume_decision <= 0:
        volume_decision = _partial(volume_weight, 0.50)
    rows.append(
        {
            "key": "volume",
            "label": LABELS["volume"],
            "score": volume_display,
            "decision_score": volume_decision if volume_enabled else 0,
            "max_score": volume_weight if volume_enabled else 0,
            "enabled": volume_enabled,
            "passed": bool(volume_enabled and (not volume_available or volume_ratio >= volume_threshold)),
            "partial": bool(volume_enabled and 0 < volume_display < volume_weight),
            "direction": "CONFIRM",
            "selected_side": candidate,
            "detail": (
                "Volume unavailable: 50% neutral display score"
                if not volume_available
                else f"Volume {volume_ratio:.2f}x / threshold {volume_threshold:.2f}x"
            ),
        }
    )

    mtf_enabled = _b(enabled.get("mtf"), True)
    mtf_ok = _b(result.get("mtf_confirmed", (market or {}).get("mtf_confirmed", False)), False)
    mtf_weight = _i(weights.get("mtf"), 0)
    mtf_display = mtf_weight if mtf_enabled and mtf_ok else 0
    mtf_decision = _i(result.get("mtf_bonus"), mtf_display)
    rows.append(
        {
            "key": "mtf",
            "label": LABELS["mtf"],
            "score": mtf_display,
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
    components = _score_components(market or {}, result or {}, profile)
    display_total = sum(_i(item.get("score"), 0) for item in components)
    decision_component_total = sum(_i(item.get("decision_score"), 0) for item in components)
    raw_total = sum(_i(item.get("max_score"), 0) for item in components if _b(item.get("enabled"), True))
    final_score = _i((result or {}).get("score"), decision_component_total)
    min_score = _i((result or {}).get("min_score", (result or {}).get("min_score_required", 82)), 82)
    return {
        "score": display_total,
        "display_score": display_total,
        "decision_score": final_score,
        "component_total": display_total,
        "decision_component_total": decision_component_total,
        "enabled_weight_total": raw_total,
        "min_score": min_score,
        "candidate_signal": str((result or {}).get("candidate_signal") or (result or {}).get("signal") or "WAIT"),
        "trade_allowed": _b((result or {}).get("trade_allowed"), False),
        "components": components,
        "warnings": list((result or {}).get("warnings", []) or [])[:8],
        "score_mode": "PARTIAL_DISPLAY_DECISION_UNCHANGED",
    }


def apply_live_score_breakdown_patch() -> None:
    if getattr(strategy_module, "_okai_live_score_breakdown_v2", False):
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
        result["score_components"] = _score_components(market_data or {}, result, profile)
        result["live_score_breakdown"] = _score_payload(market_data or {}, result, profile)
        return result

    def summary_with_breakdown(scan):
        data = original_summary(scan)
        if not isinstance(data, dict) or not isinstance(scan, dict):
            return data
        signal = scan.get("signal_data") or {}
        market = scan.get("market_data") or {}
        profile_like = {
            "weights": signal.get("profile_weights") or {},
            "enabled": signal.get("profile_enabled") or {},
            "adx_threshold": signal.get("adx_threshold", strategy_module.ADX_THRESHOLD),
            "volume_threshold": signal.get("volume_threshold", strategy_module.VOLUME_RATIO_THRESHOLD),
        }
        payload = signal.get("live_score_breakdown") or _score_payload(market, signal, profile_like)
        data.update(
            {
                "score_components": payload.get("components", []),
                "live_score_breakdown": payload,
                "display_score": payload.get("display_score"),
                "decision_score": payload.get("decision_score", data.get("score")),
                "component_total": payload.get("component_total"),
                "decision_component_total": payload.get("decision_component_total"),
                "enabled_weight_total": payload.get("enabled_weight_total"),
                "adx_threshold": profile_like["adx_threshold"],
                "volume_threshold": profile_like["volume_threshold"],
                "profile_weights": profile_like["weights"],
                "profile_enabled": profile_like["enabled"],
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

    strategy_module._okai_live_score_breakdown_v2 = True
    strategy_module._okai_live_score_breakdown_v1 = True
