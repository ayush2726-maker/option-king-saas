"""Add exact option-premium context to the adaptive shadow model.

Only information known at decision time is used. Historical rows safely receive
zero for these newly introduced fields; new snapshots carry live ATM CE/PE
premium, delta/theta pressure and premium skew. No execution behavior changes.
"""
from __future__ import annotations

import math
from typing import Any

VERSION = "OKAI-OPTION-PREMIUM-LEARNING-V3"
FEATURES = (
    "ce_premium_pct_spot",
    "pe_premium_pct_spot",
    "premium_skew",
    "ce_abs_delta",
    "pe_abs_delta",
    "ce_theta_pressure",
    "pe_theta_pressure",
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _clip(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(v)))


def apply_option_premium_learning_v3_patch() -> bool:
    try:
        from bot import adaptive_model_v2 as model
        from bot.broker_intelligence import selected_contract
        if getattr(model, "OPTION_PREMIUM_LEARNING_V3_APPLIED", False):
            return True
        previous = model.feature_vector

        def feature_vector_with_premium(*, market, base, option, news, global_market):
            out = dict(previous(
                market=market,
                base=base,
                option=option,
                news=news,
                global_market=global_market,
            ))
            spot = max(1.0, _f(market.get("price") or market.get("spot"), 1.0))
            ce = dict(selected_contract(option, "CE") or {})
            pe = dict(selected_contract(option, "PE") or {})
            ce_p = _f(ce.get("ask") or ce.get("ltp"))
            pe_p = _f(pe.get("ask") or pe.get("ltp"))
            premium_sum = ce_p + pe_p
            ce_theta = abs(_f(ce.get("theta")))
            pe_theta = abs(_f(pe.get("theta")))
            out.update({
                "ce_premium_pct_spot": _clip((ce_p / spot) / 0.05),
                "pe_premium_pct_spot": _clip((pe_p / spot) / 0.05),
                "premium_skew": _clip(abs(ce_p - pe_p) / premium_sum) if premium_sum > 0 else 0.0,
                "ce_abs_delta": _clip(abs(_f(ce.get("delta")))),
                "pe_abs_delta": _clip(abs(_f(pe.get("delta")))),
                "ce_theta_pressure": _clip(ce_theta / max(1.0, ce_p) / 0.05) if ce_p > 0 else 0.0,
                "pe_theta_pressure": _clip(pe_theta / max(1.0, pe_p) / 0.05) if pe_p > 0 else 0.0,
            })
            return out

        model.feature_vector = feature_vector_with_premium
        model.FEATURE_NAMES.extend(name for name in FEATURES if name not in model.FEATURE_NAMES)
        model.FEATURE_GROUPS["OPTION_PREMIUM"] = FEATURES
        model.VERSION = f"{model.VERSION}+{VERSION}"
        model.OPTION_PREMIUM_LEARNING_V3_APPLIED = True
        return True
    except Exception:
        return False


__all__ = ["apply_option_premium_learning_v3_patch", "VERSION"]
