"""Market-mechanics learning V4 for Option King AI.

Adds leakage-safe, decision-time features that help the adaptive model learn
*why* an index/option is moving: trend/momentum/volume, derivatives pressure,
volatility, news/global context and option premium response.  This is learning
only; it does not bypass validation or directly place/block orders.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping

VERSION = "OKAI-MARKET-MECHANICS-V4.1"

FEATURES = [
    "trend_momentum", "trend_exhaustion", "participation_strength",
    "derivatives_bull_pressure", "derivatives_bear_pressure",
    "volatility_expansion", "event_shock", "context_conflict",
    "premium_expensive", "premium_asymmetry", "theta_risk_context",
    "direction_quality_ce", "direction_quality_pe", "no_trade_pressure",
]

_NEWS_DERIVED_FEATURES = (
    "event_shock",
    "context_conflict",
    "direction_quality_ce",
    "direction_quality_pe",
)


def _f(v: Any, d: float = 0.0) -> float:
    try:
        n = float(v)
        return n if math.isfinite(n) else d
    except (TypeError, ValueError):
        return d


def _c(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _derive(f: Mapping[str, Any]) -> Dict[str, float]:
    rsi = _c(_f(f.get("rsi"), .5))
    adx = _c(_f(f.get("adx")))
    vol = _c(_f(f.get("volume_ratio")))
    atr = _c(_f(f.get("atr_percent")))
    vix = _c(_f(f.get("india_vix")))
    vix_chg = max(-1.0, min(1.0, _f(f.get("india_vix_change")) / .3))
    news = _c(_f(f.get("news_strength")))
    news_risk = _c(_f(f.get("news_risk")))
    news_ce, news_pe = _c(_f(f.get("news_ce"))), _c(_f(f.get("news_pe")))
    opt_ce, opt_pe = _c(_f(f.get("option_ce"))), _c(_f(f.get("option_pe")))
    oi = max(-1.0, min(1.0, _f(f.get("oi_direction"))))
    depth = max(-1.0, min(1.0, _f(f.get("depth_imbalance"))))
    pcr = _c(_f(f.get("pcr"), 1/3))
    spread = _c(_f(f.get("spread_percent")))
    risk = _c(_f(f.get("option_risk")))
    iv = _c(_f(f.get("average_iv")))
    # Option-premium V3 publishes the canonical ``*_premium_pct_spot`` names.
    # Keep the old aliases as a read fallback for any already persisted rows.
    ce_prem = _c(_f(f.get("ce_premium_pct_spot", f.get("ce_premium_to_spot"))))
    pe_prem = _c(_f(f.get("pe_premium_pct_spot", f.get("pe_premium_to_spot"))))
    ce_theta = _c(_f(f.get("ce_theta_pressure")))
    pe_theta = _c(_f(f.get("pe_theta_pressure")))

    momentum = _c(adx * (abs(rsi - .5) * 2.0) * (.5 + .5 * vol))
    exhaustion = _c(adx * max(0.0, abs(rsi - .5) * 2.0 - .65))
    participation = _c((vol + abs(depth) + abs(oi)) / 3.0)
    deriv_bull = _c((max(0.0, oi) + max(0.0, depth) + opt_ce + max(0.0, (1/3-pcr)*3)) / 4.0)
    deriv_bear = _c((max(0.0, -oi) + max(0.0, -depth) + opt_pe + max(0.0, (pcr-1/3)*3)) / 4.0)
    vol_expand = _c((atr + vix + max(0.0, vix_chg)) / 3.0)
    shock = _c((news + news_risk + max(0.0, vix_chg)) / 3.0)
    conflict = _c(abs(deriv_bull - news_ce) + abs(deriv_bear - news_pe)) / 2.0
    expensive = _c((iv + max(ce_prem, pe_prem) + risk + spread) / 4.0)
    asymmetry = _c(abs(ce_prem - pe_prem) * 2.0)
    theta = _c(max(ce_theta, pe_theta))
    ce_quality = _c((momentum + participation + deriv_bull + news_ce) / 4.0 * (1.0 - .45 * expensive))
    pe_quality = _c((momentum + participation + deriv_bear + news_pe) / 4.0 * (1.0 - .45 * expensive))
    no_trade = _c((conflict + expensive + theta + risk + spread) / 5.0)
    return {
        "trend_momentum": momentum, "trend_exhaustion": exhaustion,
        "participation_strength": participation,
        "derivatives_bull_pressure": deriv_bull,
        "derivatives_bear_pressure": deriv_bear,
        "volatility_expansion": vol_expand, "event_shock": shock,
        "context_conflict": conflict, "premium_expensive": expensive,
        "premium_asymmetry": asymmetry, "theta_risk_context": theta,
        "direction_quality_ce": ce_quality, "direction_quality_pe": pe_quality,
        "no_trade_pressure": no_trade,
    }


def apply_market_mechanics_learning_v4_patch() -> bool:
    try:
        from bot import adaptive_model_v2 as model
        if getattr(model, "MARKET_MECHANICS_V4_APPLIED", False):
            return True
        original = model.feature_vector
        def feature_vector_v4(*, market, base, option, news, global_market):
            f = dict(original(market=market, base=base, option=option, news=news, global_market=global_market))
            f.update(_derive(f))
            return f
        model.feature_vector = feature_vector_v4
        for name in FEATURES:
            if name not in model.FEATURE_NAMES:
                model.FEATURE_NAMES.append(name)
        model.FEATURE_GROUPS["MARKET_MECHANICS"] = tuple(FEATURES)
        ablation = tuple(getattr(model, "NEWS_ABLATION_FEATURES", model.NEWS_FEATURES))
        model.NEWS_ABLATION_FEATURES = tuple(dict.fromkeys(ablation + _NEWS_DERIVED_FEATURES))

        # Extend the V3 historical-row wrapper: V3 already reconstructs its own
        # derived features from stored snapshots; derive V4 from those rows too.
        previous_rows = model._training_rows
        def rows_v4(horizon):
            x, y = previous_rows(horizon)
            names_without_v4 = [n for n in model.FEATURE_NAMES if n not in FEATURES]
            out = []
            for values in x:
                f = {n: _f(values[i]) for i, n in enumerate(names_without_v4) if i < len(values)}
                f.update(_derive(f))
                out.append([_f(f.get(n)) for n in model.FEATURE_NAMES])
            return out, y
        model._training_rows = rows_v4
        model.VERSION = f"{model.VERSION}+{VERSION}"
        model.MARKET_MECHANICS_V4_APPLIED = True
        return True
    except Exception:
        return False

__all__ = ["apply_market_mechanics_learning_v4_patch", "VERSION"]
