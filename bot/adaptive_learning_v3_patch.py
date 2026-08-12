"""Adaptive-learning V3 patch for Option King AI.

Improves the existing shadow-only adaptive model without touching live entry,
exit, sizing, broker, or order logic.  The patch adds leakage-safe interaction
features (regime, agreement/conflict, news context, volatility/liquidity) and a
chronological inner-validation fit so the softmax is less prone to overfitting.

Missed-trade samples are already written into ai_advanced_v2_* by
missed_trade_learning_v1; this patch deliberately keeps them in the same
chronological dataset so both MISSED_PROFIT and BLOCK_AVOIDED_LOSS examples are
learned from when they are training-eligible.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping

VERSION = "OKAI-ADAPTIVE-LEARNING-V3"

_DERIVED_FEATURES = [
    "base_option_ce_agree",
    "base_option_pe_agree",
    "base_option_no_trade_agree",
    "direction_conflict",
    "news_ce_alignment",
    "news_pe_alignment",
    "bullish_context",
    "bearish_context",
    "trend_strength",
    "volatility_pressure",
    "liquidity_risk",
    "pcr_bullish_pressure",
    "pcr_bearish_pressure",
    "directional_edge",
]


def _f(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _derived(feature: Mapping[str, Any]) -> Dict[str, float]:
    """Create only contemporaneous features; no future/outcome fields allowed."""
    base_ce = _clip(_f(feature.get("base_ce")))
    base_pe = _clip(_f(feature.get("base_pe")))
    base_nt = _clip(_f(feature.get("base_no_trade")))
    opt_ce = _clip(_f(feature.get("option_ce")))
    opt_pe = _clip(_f(feature.get("option_pe")))
    opt_nt = _clip(_f(feature.get("option_no_trade")))
    news_ce = _clip(_f(feature.get("news_ce")))
    news_pe = _clip(_f(feature.get("news_pe")))
    rsi = _clip(_f(feature.get("rsi"), 0.5))
    adx = _clip(_f(feature.get("adx")))
    atr = _clip(_f(feature.get("atr_percent")))
    vix = _clip(_f(feature.get("india_vix")))
    option_risk = _clip(_f(feature.get("option_risk")))
    coverage = _clip(_f(feature.get("coverage")))
    spread = _clip(_f(feature.get("spread_percent")))
    pcr_norm = _clip(_f(feature.get("pcr"), 1.0 / 3.0))

    bullish_votes = (base_ce + opt_ce + news_ce + rsi) / 4.0
    bearish_votes = (base_pe + opt_pe + news_pe + (1.0 - rsi)) / 4.0
    signed_base = base_ce - base_pe
    signed_opt = opt_ce - opt_pe

    return {
        "base_option_ce_agree": min(base_ce, opt_ce),
        "base_option_pe_agree": min(base_pe, opt_pe),
        "base_option_no_trade_agree": min(base_nt, opt_nt),
        "direction_conflict": _clip(abs(signed_base - signed_opt) / 2.0),
        "news_ce_alignment": min(news_ce, max(base_ce, opt_ce)),
        "news_pe_alignment": min(news_pe, max(base_pe, opt_pe)),
        "bullish_context": _clip(bullish_votes),
        "bearish_context": _clip(bearish_votes),
        "trend_strength": adx,
        "volatility_pressure": max(atr, vix),
        "liquidity_risk": _clip((option_risk + spread + (1.0 - coverage)) / 3.0),
        # adaptive_model_v2 stores PCR divided by 3, so neutral ~0.333.
        "pcr_bullish_pressure": _clip((1.0 / 3.0 - pcr_norm) * 3.0),
        "pcr_bearish_pressure": _clip((pcr_norm - 1.0 / 3.0) * 3.0),
        "directional_edge": _clip(abs(bullish_votes - bearish_votes)),
    }


def _install_feature_patch(model) -> None:
    original_feature_vector = model.feature_vector

    def feature_vector_v3(*, market, base, option, news, global_market):
        feature = dict(original_feature_vector(
            market=market,
            base=base,
            option=option,
            news=news,
            global_market=global_market,
        ))
        feature.update(_derived(feature))
        return feature

    model.feature_vector = feature_vector_v3

    # Rebuild historical derived values from the already persisted base features.
    original_training_rows = model._training_rows

    def training_rows_v3(horizon):
        x_old, y = original_training_rows(horizon)
        old_names = [name for name in model.FEATURE_NAMES if name not in _DERIVED_FEATURES]
        rows = []
        for values in x_old:
            raw = {name: _f(values[idx]) for idx, name in enumerate(old_names) if idx < len(values)}
            raw.update(_derived(raw))
            rows.append([_f(raw.get(name)) for name in model.FEATURE_NAMES])
        return rows, y

    # Important: original _training_rows reads model.FEATURE_NAMES dynamically.
    # Wrap it with a stable pre-V3 name list to avoid dimension mismatch.
    base_names = list(model.FEATURE_NAMES)

    def training_rows_v3_safe(horizon):
        conn = model.get_db()
        try:
            db_rows = conn.execute(
                """SELECT s.feature_json,o.best_label
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
        label_index = {label: index for index, label in enumerate(model.LABELS)}
        x_rows, y_rows = [], []
        for row in db_rows:
            raw = model._loads(row["feature_json"], {})
            raw = {name: _f(raw.get(name)) for name in base_names}
            raw.update(_derived(raw))
            x_rows.append([_f(raw.get(name)) for name in model.FEATURE_NAMES])
            y_rows.append(label_index[str(row["best_label"])])
        return x_rows, y_rows

    model.FEATURE_NAMES.extend(name for name in _DERIVED_FEATURES if name not in model.FEATURE_NAMES)
    model.FEATURE_GROUPS["REGIME_INTERACTIONS"] = tuple(_DERIVED_FEATURES)
    model._training_rows = training_rows_v3_safe


def _install_fit_patch(model) -> None:
    """Tune on the tail of TRAINING only, never on the final validation holdout."""
    def fit_softmax_v3(np, x_train, y_train):
        classes = len(model.LABELS)
        n = int(len(y_train))
        if n < 80:
            return _fit_once(np, x_train, y_train, classes, 0.025, 0.008, 750)

        inner_n = max(30, int(n * 0.15))
        split = max(40, n - inner_n)
        x_fit, y_fit = x_train[:split], y_train[:split]
        x_check, y_check = x_train[split:], y_train[split:]
        candidates = (
            (0.018, 0.015, 650),
            (0.025, 0.010, 750),
            (0.032, 0.006, 850),
            (0.040, 0.004, 900),
        )
        best = None
        for lr, l2, steps in candidates:
            w, b = _fit_once(np, x_fit, y_fit, classes, lr, l2, steps)
            p = model._predict_probabilities(np, x_check, w, b)
            accuracy, brier = model._metrics(np, p, y_check)
            # Calibration matters as well as hit-rate; lower score is better.
            score = (1.0 - accuracy) + 0.35 * brier
            if best is None or score < best[0]:
                best = (score, lr, l2, steps)
        _, lr, l2, steps = best
        return _fit_once(np, x_train, y_train, classes, lr, l2, steps)

    model._fit_softmax = fit_softmax_v3


def _fit_once(np, x_train, y_train, classes, lr, l2, steps):
    weights = np.zeros((x_train.shape[1], classes))
    bias = np.zeros(classes)
    target = np.eye(classes)[y_train]
    counts = np.bincount(y_train, minlength=classes).astype(float)
    class_weights = counts.sum() / np.maximum(1.0, classes * counts)
    row_weights = class_weights[y_train][:, None]
    for _ in range(int(steps)):
        logits = x_train @ weights + bias
        logits -= logits.max(axis=1, keepdims=True)
        exp = np.exp(logits)
        probabilities = exp / exp.sum(axis=1, keepdims=True)
        error = (probabilities - target) * row_weights
        weights -= lr * (x_train.T @ error / len(x_train) + l2 * weights)
        bias -= lr * error.mean(axis=0)
    return weights, bias


def apply_adaptive_learning_v3_patch() -> bool:
    try:
        from bot import adaptive_model_v2 as model
        if getattr(model, "ADAPTIVE_LEARNING_V3_APPLIED", False):
            return True
        _install_feature_patch(model)
        _install_fit_patch(model)
        model.VERSION = f"{model.VERSION}+{VERSION}"
        model.ADAPTIVE_LEARNING_V3_APPLIED = True
        return True
    except Exception:
        return False


__all__ = ["apply_adaptive_learning_v3_patch", "VERSION"]
