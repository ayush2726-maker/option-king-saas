"""Teach the shadow model from the real baseline setup without giving it authority.

The baseline strategy remains the only entry/direction/score/exit authority.
This patch adds decision-time setup context to the adaptive training features so
the shadow model learns when a qualified baseline setup tends to work or fail.
No future outcome field is used while constructing features.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping


VERSION = "OKAI-BASELINE-SETUP-TRAINING-V5"
FEATURES = [
    "setup_candidate_ce",
    "setup_candidate_pe",
    "setup_candidate_no_trade",
    "setup_score",
    "setup_min_score",
    "setup_score_margin",
    "setup_trade_allowed",
    "setup_execution_allowed",
    "setup_qualified",
    "setup_base_alignment",
    "setup_indicator_alignment",
    "setup_orb_alignment",
]


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _direction(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "CE" in text or text in {"BUY", "UP", "UPTREND", "BULLISH", "CALL"}:
        return "CE"
    if "PE" in text or text in {"SELL", "DOWN", "DOWNTREND", "BEARISH", "PUT"}:
        return "PE"
    return "NO_TRADE"


def _same_direction(value: Any, candidate: str) -> float:
    direction = _direction(value)
    return 1.0 if candidate in {"CE", "PE"} and direction == candidate else 0.0


def _value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        try:
            value = row.get(key, default)
            return default if value is None else value
        except Exception:
            return default


def _setup_features(market: Mapping[str, Any], base: Mapping[str, Any]) -> Dict[str, float]:
    candidate = _direction(
        market.get("signal_direction")
        or market.get("signal")
        or market.get("candidate_side")
    )
    score = _f(market.get("strategy_score"), _f(market.get("score")))
    minimum = _f(
        market.get("min_strategy_score"),
        _f(market.get("min_score"), 82.0),
    )
    trade_allowed = bool(
        market.get("server_trade_allowed", market.get("trade_allowed", False))
    )
    execution_allowed = bool(
        market.get("execution_allowed", trade_allowed)
    )
    qualified = bool(candidate in {"CE", "PE"} and score >= minimum)

    base_probs = dict(base.get("probabilities") or {})
    candidate_probability = _f(base_probs.get(candidate)) if candidate in {"CE", "PE"} else 0.0
    opposite_probability = _f(
        base_probs.get("PE" if candidate == "CE" else "CE")
    ) if candidate in {"CE", "PE"} else 0.0
    base_alignment = max(-1.0, min(1.0, (candidate_probability - opposite_probability) / 100.0))

    price = _f(market.get("price"), _f(market.get("spot")))
    ema_fast = _f(market.get("ema_fast"), _f(market.get("ema9"), price))
    ema_slow = _f(market.get("ema_slow"), _f(market.get("ema21"), price))
    vwap = _f(market.get("vwap"), price)
    directional_checks = [
        1.0 if candidate == "CE" and ema_fast > ema_slow else 1.0 if candidate == "PE" and ema_fast < ema_slow else 0.0,
        1.0 if candidate == "CE" and price >= vwap else 1.0 if candidate == "PE" and price <= vwap else 0.0,
        _same_direction(market.get("supertrend_direction"), candidate),
        _same_direction(market.get("mtf_direction"), candidate),
        _same_direction(market.get("structure_direction"), candidate),
    ]
    indicator_alignment = sum(directional_checks) / len(directional_checks)

    orb_high = _f(market.get("orb_high"))
    orb_low = _f(market.get("orb_low"))
    if candidate == "CE" and orb_high > 0:
        orb_alignment = 1.0 if price > orb_high + 5.0 else 0.0
    elif candidate == "PE" and orb_low > 0:
        orb_alignment = 1.0 if price < orb_low - 5.0 else 0.0
    else:
        orb_alignment = 0.0

    return {
        "setup_candidate_ce": 1.0 if candidate == "CE" else 0.0,
        "setup_candidate_pe": 1.0 if candidate == "PE" else 0.0,
        "setup_candidate_no_trade": 1.0 if candidate == "NO_TRADE" else 0.0,
        "setup_score": _clip(score / 100.0),
        "setup_min_score": _clip(minimum / 100.0),
        "setup_score_margin": max(-1.0, min(1.0, (score - minimum) / 30.0)),
        "setup_trade_allowed": 1.0 if trade_allowed else 0.0,
        "setup_execution_allowed": 1.0 if execution_allowed else 0.0,
        "setup_qualified": 1.0 if qualified else 0.0,
        "setup_base_alignment": base_alignment,
        "setup_indicator_alignment": _clip(indicator_alignment),
        "setup_orb_alignment": orb_alignment,
    }


def _historical_setup(stored: Mapping[str, Any], persisted: Mapping[str, Any]) -> Dict[str, float]:
    candidate = _direction(_value(stored, "strategy_candidate_side"))
    score = _f(_value(stored, "strategy_score"))
    minimum = _f(_value(stored, "strategy_min_score"), 82.0)
    reconstructed = {
        "setup_candidate_ce": 1.0 if candidate == "CE" else 0.0,
        "setup_candidate_pe": 1.0 if candidate == "PE" else 0.0,
        "setup_candidate_no_trade": 1.0 if candidate == "NO_TRADE" else 0.0,
        "setup_score": _clip(score / 100.0),
        "setup_min_score": _clip(minimum / 100.0),
        "setup_score_margin": max(-1.0, min(1.0, (score - minimum) / 30.0)),
        "setup_trade_allowed": 1.0 if _value(stored, "strategy_trade_allowed") else 0.0,
        "setup_execution_allowed": 1.0 if _value(stored, "strategy_execution_allowed") else 0.0,
        "setup_qualified": 1.0 if candidate in {"CE", "PE"} and score >= minimum else 0.0,
        "setup_base_alignment": 0.0,
        "setup_indicator_alignment": 0.0,
        "setup_orb_alignment": 0.0,
    }
    return {
        name: _f(persisted.get(name), reconstructed[name])
        for name in FEATURES
    }


def apply_baseline_setup_training_v5_patch() -> bool:
    try:
        from bot import adaptive_model_v2 as model

        if getattr(model, "BASELINE_SETUP_TRAINING_V5_APPLIED", False):
            return True

        previous_vector = model.feature_vector
        previous_rows = model._training_rows

        def feature_vector_v5(*, market, base, option, news, global_market):
            output = dict(previous_vector(
                market=market,
                base=base,
                option=option,
                news=news,
                global_market=global_market,
            ))
            output.update(_setup_features(market, base))
            return output

        model.feature_vector = feature_vector_v5
        model.FEATURE_NAMES.extend(name for name in FEATURES if name not in model.FEATURE_NAMES)
        model.FEATURE_GROUPS["BASELINE_SETUP"] = tuple(FEATURES)

        def training_rows_v5(horizon):
            x_rows, labels = previous_rows(horizon)
            conn = model.get_db()
            try:
                stored_rows = conn.execute(
                    """SELECT s.feature_json,s.strategy_candidate_side,
                    s.strategy_score,s.strategy_min_score,
                    s.strategy_trade_allowed,s.strategy_execution_allowed
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
                return x_rows, labels

            indices = {name: model.FEATURE_NAMES.index(name) for name in FEATURES}
            rebuilt = []
            for values, stored in zip(x_rows, stored_rows):
                row_values = list(values)
                if len(row_values) < len(model.FEATURE_NAMES):
                    row_values.extend([0.0] * (len(model.FEATURE_NAMES) - len(row_values)))
                persisted = model._loads(stored["feature_json"], {})
                calculated = _historical_setup(stored, persisted)
                for name, index in indices.items():
                    row_values[index] = calculated[name]
                rebuilt.append(row_values)
            return rebuilt, labels

        model._training_rows = training_rows_v5
        model.VERSION = f"{model.VERSION}+{VERSION}"
        model.BASELINE_SETUP_TRAINING_V5_APPLIED = True
        model.TRAINING_ONLY = True
        model.DECISION_AUTHORITY = "BASELINE_STRATEGY_ONLY"
        return True
    except Exception:
        return False


__all__ = [
    "apply_baseline_setup_training_v5_patch",
    "FEATURES",
    "VERSION",
]
