"""Add exact option-premium context to the adaptive shadow model.

Only information known at decision time is used. Historical rows reconstruct
these fields from their stored entry spot and ATM CE/PE contract snapshots;
new snapshots carry them directly. No execution behavior changes.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

VERSION = "OKAI-OPTION-PREMIUM-LEARNING-V3.1"
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


def _premium_features(
    spot_value: Any,
    ce_value: Mapping[str, Any],
    pe_value: Mapping[str, Any],
):
    """Build normalized premium fields from decision-time contract snapshots."""
    spot = max(1.0, _f(spot_value, 1.0))
    ce, pe = dict(ce_value or {}), dict(pe_value or {})
    ce_p = _f(ce.get("ask") or ce.get("ltp"))
    pe_p = _f(pe.get("ask") or pe.get("ltp"))
    premium_sum = ce_p + pe_p
    ce_theta = abs(_f(ce.get("theta")))
    pe_theta = abs(_f(pe.get("theta")))
    return {
        "ce_premium_pct_spot": _clip((ce_p / spot) / 0.05),
        "pe_premium_pct_spot": _clip((pe_p / spot) / 0.05),
        "premium_skew": _clip(abs(ce_p - pe_p) / premium_sum) if premium_sum > 0 else 0.0,
        "ce_abs_delta": _clip(abs(_f(ce.get("delta")))),
        "pe_abs_delta": _clip(abs(_f(pe.get("delta")))),
        "ce_theta_pressure": _clip(ce_theta / max(1.0, ce_p) / 0.05) if ce_p > 0 else 0.0,
        "pe_theta_pressure": _clip(pe_theta / max(1.0, pe_p) / 0.05) if pe_p > 0 else 0.0,
    }


def apply_option_premium_learning_v3_patch() -> bool:
    try:
        from bot import adaptive_model_v2 as model
        from bot.broker_intelligence import selected_contract
        if getattr(model, "OPTION_PREMIUM_LEARNING_V3_APPLIED", False):
            return True
        previous = model.feature_vector
        previous_rows = model._training_rows

        def feature_vector_with_premium(*, market, base, option, news, global_market):
            out = dict(previous(
                market=market,
                base=base,
                option=option,
                news=news,
                global_market=global_market,
            ))
            ce = dict(selected_contract(option, "CE") or {})
            pe = dict(selected_contract(option, "PE") or {})
            out.update(_premium_features(
                market.get("price") or market.get("spot"),
                ce,
                pe,
            ))
            return out

        model.feature_vector = feature_vector_with_premium
        model.FEATURE_NAMES.extend(name for name in FEATURES if name not in model.FEATURE_NAMES)
        model.FEATURE_GROUPS["OPTION_PREMIUM"] = FEATURES

        def training_rows_with_premium(horizon):
            """Backfill premium fields from stored entry contracts.

            Older feature_json rows predate V3, but their decision-time spot and
            CE/PE contracts were already stored. Reconstructing from those
            immutable snapshots adds real historical variation without using
            any future quote or outcome information.
            """
            x_rows, y_rows = previous_rows(horizon)
            conn = model.get_db()
            try:
                stored_rows = conn.execute(
                    """SELECT s.spot,s.feature_json,s.ce_contract_json,s.pe_contract_json
                    FROM ai_advanced_v2_snapshots s
                    JOIN ai_advanced_v2_contract_outcomes o ON o.decision_id=s.id
                    WHERE o.horizon_minutes=? AND o.best_label IN('CE','PE','NO_TRADE')
                      AND COALESCE(s.learning_eligible,1)=1
                      AND COALESCE(o.training_eligible,1)=1
                    ORDER BY datetime(s.created_at),s.rowid""",
                    (horizon,),
                ).fetchall()
            finally:
                conn.close()
            if len(stored_rows) != len(x_rows):
                return x_rows, y_rows

            indices = {name: model.FEATURE_NAMES.index(name) for name in FEATURES}
            rebuilt = []
            for values, stored in zip(x_rows, stored_rows):
                row_values = list(values)
                if len(row_values) < len(model.FEATURE_NAMES):
                    row_values.extend([0.0] * (len(model.FEATURE_NAMES) - len(row_values)))
                persisted = model._loads(stored["feature_json"], {})
                calculated = _premium_features(
                    stored["spot"],
                    model._loads(stored["ce_contract_json"], {}),
                    model._loads(stored["pe_contract_json"], {}),
                )
                for name, index in indices.items():
                    row_values[index] = _f(persisted.get(name), calculated[name])
                rebuilt.append(row_values)
            return rebuilt, y_rows

        model._training_rows = training_rows_with_premium
        model.VERSION = f"{model.VERSION}+{VERSION}"
        model.OPTION_PREMIUM_LEARNING_V3_APPLIED = True
        return True
    except Exception:
        return False


__all__ = ["apply_option_premium_learning_v3_patch", "VERSION"]
