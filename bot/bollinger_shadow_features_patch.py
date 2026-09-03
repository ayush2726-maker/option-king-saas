"""Bollinger-derived shadow features for the adaptive prediction model.

Observation/learning only: this patch does not alter strategy gates, score,
orders, position sizing, stop loss, or exits.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

PERIODS = (9, 14, 20)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _series(market: Mapping[str, Any]):
    for key in ("closes", "close_series", "close_history", "recent_closes"):
        raw = market.get(key)
        if isinstance(raw, (list, tuple)):
            values = [_f(v, float("nan")) for v in raw]
            values = [v for v in values if math.isfinite(v) and v > 0]
            if values:
                return values
    candles = market.get("candles") or market.get("recent_candles") or []
    if isinstance(candles, (list, tuple)):
        values = [_f((c or {}).get("close"), float("nan")) for c in candles if isinstance(c, Mapping)]
        return [v for v in values if math.isfinite(v) and v > 0]
    return []


def _bb(closes, period: int):
    if len(closes) < period:
        return {"position": 0.5, "mid_distance": 0.0, "width": 0.0, "mid_slope": 0.0}
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = math.sqrt(max(0.0, variance))
    upper, lower = mid + 2.0 * std, mid - 2.0 * std
    price = closes[-1]
    width_abs = max(0.0, upper - lower)
    position = (price - lower) / width_abs if width_abs > 1e-12 else 0.5
    prev_mid = sum(closes[-period-1:-1]) / period if len(closes) > period else mid
    return {
        "position": max(-0.5, min(1.5, position)),
        "mid_distance": max(-0.10, min(0.10, (price - mid) / mid if mid else 0.0)),
        "width": max(0.0, min(0.20, width_abs / mid if mid else 0.0)),
        "mid_slope": max(-0.05, min(0.05, (mid - prev_mid) / prev_mid if prev_mid else 0.0)),
    }


def apply_bollinger_shadow_features_patch() -> None:
    from bot import adaptive_model_v2 as model
    if getattr(model, "_okai_bollinger_shadow_features_v1", False):
        return

    names = []
    for p in PERIODS:
        names += [f"bb{p}_position", f"bb{p}_mid_distance", f"bb{p}_width", f"bb{p}_mid_slope"]
    for name in names:
        if name not in model.FEATURE_NAMES:
            model.FEATURE_NAMES.append(name)
    model.FEATURE_GROUPS["BOLLINGER_SHADOW"] = tuple(names)

    original = model.feature_vector

    def wrapped(*, market, base, option, news, global_market):
        out = dict(original(market=market, base=base, option=option, news=news, global_market=global_market))
        closes = _series(market)
        for p in PERIODS:
            values = _bb(closes, p)
            out[f"bb{p}_position"] = values["position"]
            out[f"bb{p}_mid_distance"] = values["mid_distance"]
            out[f"bb{p}_width"] = values["width"]
            out[f"bb{p}_mid_slope"] = values["mid_slope"]
        return out

    model.feature_vector = wrapped
    model.VERSION = str(model.VERSION) + "+BB-SHADOW-9-14-20"
    model._okai_bollinger_shadow_features_v1 = True
