"""Leakage-safe adaptive classifier for exact option outcomes.

The model trains chronologically, validates on the newest holdout and remains
shadow-only. Failed models keep learning but never affect trade execution.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Tuple

from database import get_db

VERSION = "OKAI-ADAPTIVE-SOFTMAX-V2.1"
MIN_TRAINING_SAMPLES = 300
MIN_VALIDATION_SAMPLES = 60
MIN_VALIDATION_ACCURACY = 0.40
MIN_EDGE_OVER_BASELINE = 0.02
MAX_BRIER_SCORE = 0.68
MIN_CLASS_TRAIN_SAMPLES = 20
TRAIN_INTERVAL_SECONDS = 6 * 60 * 60
FEATURE_NAMES = [
    "base_ce", "base_pe", "base_no_trade", "option_ce", "option_pe",
    "option_no_trade", "option_confidence", "option_risk", "coverage",
    "pcr", "oi_direction", "depth_imbalance", "spread_percent",
    "average_iv", "news_ce", "news_pe", "news_strength", "news_risk",
    "india_vix", "india_vix_change", "adx", "rsi", "atr_percent",
    "volume_ratio",
]
LABELS = ("CE", "PE", "NO_TRADE")
NEWS_FEATURES = ("news_ce", "news_pe", "news_strength", "news_risk")
FEATURE_GROUPS = {
    "BASE_STRATEGY": ("base_ce", "base_pe", "base_no_trade"),
    "OPTION_CHAIN": (
        "option_ce", "option_pe", "option_no_trade", "option_confidence",
        "option_risk", "coverage", "pcr", "oi_direction",
        "depth_imbalance", "spread_percent", "average_iv",
    ),
    "NEWS": NEWS_FEATURES,
    "GLOBAL": ("india_vix", "india_vix_change"),
    "MARKET": ("adx", "rsi", "atr_percent", "volume_ratio"),
}
_last_train = 0.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _display_status(status: str, sample_count: int) -> str:
    raw = str(status or "COLLECTING").upper()
    if raw == "ACTIVE_SHADOW":
        return "VALIDATED_SHADOW"
    if raw == "VALIDATION_FAILED":
        return "REJECTED_RETRAINING"
    if raw == "ERROR":
        return "ERROR"
    if sample_count >= MIN_TRAINING_SAMPLES:
        return "RETRAINING"
    return "COLLECTING"


def _status_explanation(status: str, sample_count: int, diagnostics: Mapping[str, Any]) -> str:
    display = _display_status(status, sample_count)
    if display == "VALIDATED_SHADOW":
        return "Validation pass hua; model abhi bhi monitor-only shadow mode me hai."
    if display == "REJECTED_RETRAINING":
        failures = list((diagnostics.get("activation_gate") or {}).get("failed_checks") or [])
        reason = ", ".join(str(item) for item in failures[:3])
        return "Validation fail hua; model trade me use nahi ho raha aur naye data par retrain hoga" + (f": {reason}" if reason else ".")
    if display == "RETRAINING":
        return "Minimum samples poore hain; agla chronological validation/retrain pending hai."
    if display == "ERROR":
        return "Model training error aaya; trade blocking aur order execution phir bhi OFF hain."
    return f"Abhi {sample_count}/{MIN_TRAINING_SAMPLES} valid samples collect ho rahe hain."


def ensure_model_schema() -> None:
    conn = get_db()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_adaptive_models_v2(
          horizon_minutes INTEGER PRIMARY KEY,model_version TEXT NOT NULL,
          status TEXT NOT NULL,trained_at TEXT,sample_count INTEGER DEFAULT 0,
          train_count INTEGER DEFAULT 0,validation_count INTEGER DEFAULT 0,
          validation_accuracy REAL,baseline_accuracy REAL,brier_score REAL,
          feature_names_json TEXT NOT NULL DEFAULT '[]',means_json TEXT NOT NULL DEFAULT '[]',
          scales_json TEXT NOT NULL DEFAULT '[]',weights_json TEXT NOT NULL DEFAULT '[]',
          bias_json TEXT NOT NULL DEFAULT '[]',last_error TEXT,
          diagnostics_json TEXT NOT NULL DEFAULT '{}');
        """)
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(ai_adaptive_models_v2)").fetchall()}
        if "diagnostics_json" not in columns:
            conn.execute("ALTER TABLE ai_adaptive_models_v2 ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}'")
        for horizon in (5, 15, 30):
            conn.execute(
                """INSERT OR IGNORE INTO ai_adaptive_models_v2(
                horizon_minutes,model_version,status,feature_names_json,diagnostics_json)
                VALUES(?,?,'COLLECTING',?,'{}')""",
                (horizon, VERSION, _dumps(FEATURE_NAMES)),
            )
        conn.commit()
    finally:
        conn.close()


def feature_vector(
    *,
    market: Mapping[str, Any],
    base: Mapping[str, Any],
    option: Mapping[str, Any],
    news: Mapping[str, Any],
    global_market: Mapping[str, Any],
) -> Dict[str, float]:
    base_probs = dict(base.get("probabilities") or {})
    option_direction = str(option.get("option_direction") or "NO_TRADE").upper()
    option_confidence = _f(option.get("option_confidence"))
    option_probs = {"CE": 0.0, "PE": 0.0, "NO_TRADE": 0.0}
    option_probs[option_direction if option_direction in option_probs else "NO_TRADE"] = option_confidence
    residual = max(0.0, 100.0 - option_confidence) / 2.0
    for key in option_probs:
        if key != option_direction:
            option_probs[key] = residual
    news_bias = str(news.get("news_bias") or "NEUTRAL").upper()
    news_strength = _f(news.get("news_strength"))
    vix = dict((global_market.get("values") or {}).get("india_vix") or {})
    return {
        "base_ce": _f(base_probs.get("CE")) / 100.0,
        "base_pe": _f(base_probs.get("PE")) / 100.0,
        "base_no_trade": _f(base_probs.get("NO_TRADE")) / 100.0,
        "option_ce": option_probs["CE"] / 100.0,
        "option_pe": option_probs["PE"] / 100.0,
        "option_no_trade": option_probs["NO_TRADE"] / 100.0,
        "option_confidence": option_confidence / 100.0,
        "option_risk": _f(option.get("risk_score")) / 100.0,
        "coverage": _f(option.get("data_coverage_score")) / 100.0,
        "pcr": min(3.0, max(0.0, _f(option.get("pcr"), 1.0))) / 3.0,
        "oi_direction": _f(option.get("oi_direction_score")),
        "depth_imbalance": _f(option.get("depth_imbalance")),
        "spread_percent": min(10.0, _f(option.get("average_spread_percent"))) / 10.0,
        "average_iv": min(100.0, _f(option.get("average_iv"))) / 100.0,
        "news_ce": news_strength / 100.0 if news_bias == "CE" else 0.0,
        "news_pe": news_strength / 100.0 if news_bias == "PE" else 0.0,
        "news_strength": news_strength / 100.0,
        "news_risk": _f(news.get("news_risk_score")) / 100.0,
        "india_vix": min(60.0, _f(vix.get("last_price"))) / 60.0,
        "india_vix_change": max(-0.3, min(0.3, _f(vix.get("change_percent")) / 100.0)),
        "adx": min(60.0, _f(market.get("adx"))) / 60.0,
        "rsi": min(100.0, max(0.0, _f(market.get("rsi"), 50.0))) / 100.0,
        "atr_percent": min(5.0, _f(market.get("atr_percent"))) / 5.0,
        "volume_ratio": min(4.0, _f(market.get("volume_ratio"))) / 4.0,
    }


def _training_rows(horizon: int) -> Tuple[List[List[float]], List[int]]:
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT s.feature_json,o.best_label
            FROM ai_advanced_v2_snapshots s
            JOIN ai_advanced_v2_contract_outcomes o ON o.decision_id=s.id
            WHERE o.horizon_minutes=? AND o.best_label IN('CE','PE','NO_TRADE')
            ORDER BY datetime(s.created_at),s.rowid""",
            (horizon,),
        ).fetchall()
    finally:
        conn.close()
    label_index = {label: index for index, label in enumerate(LABELS)}
    x_rows: List[List[float]] = []
    y_rows: List[int] = []
    for row in rows:
        feature = _loads(row["feature_json"], {})
        x_rows.append([_f(feature.get(name)) for name in FEATURE_NAMES])
        y_rows.append(label_index[str(row["best_label"])])
    return x_rows, y_rows


def _collecting(horizon: int, count: int, error: Any = None) -> None:
    conn = get_db()
    try:
        conn.execute(
            """UPDATE ai_adaptive_models_v2 SET model_version=?,status='COLLECTING',
            sample_count=?,last_error=?,diagnostics_json=? WHERE horizon_minutes=?""",
            (
                VERSION,
                count,
                str(error)[:300] if error else None,
                _dumps({
                    "activation_gate": {
                        "passed": False,
                        "failed_checks": ["INSUFFICIENT_SAMPLES"],
                    },
                    "sample_count": count,
                    "required_samples": MIN_TRAINING_SAMPLES,
                }),
                horizon,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _fit_softmax(np: Any, x_train: Any, y_train: Any) -> Tuple[Any, Any]:
    classes = len(LABELS)
    weights = np.zeros((x_train.shape[1], classes))
    bias = np.zeros(classes)
    target = np.eye(classes)[y_train]
    counts = np.bincount(y_train, minlength=classes).astype(float)
    class_weights = counts.sum() / np.maximum(1.0, classes * counts)
    row_weights = class_weights[y_train][:, None]
    for _ in range(900):
        logits = x_train @ weights + bias
        logits -= logits.max(axis=1, keepdims=True)
        exp = np.exp(logits)
        probabilities = exp / exp.sum(axis=1, keepdims=True)
        error = (probabilities - target) * row_weights
        weights -= 0.035 * (x_train.T @ error / len(x_train) + 0.004 * weights)
        bias -= 0.035 * error.mean(axis=0)
    return weights, bias


def _predict_probabilities(np: Any, x_values: Any, weights: Any, bias: Any) -> Any:
    logits = x_values @ weights + bias
    logits -= logits.max(axis=1, keepdims=True)
    exp = np.exp(logits)
    return exp / exp.sum(axis=1, keepdims=True)


def _metrics(np: Any, probabilities: Any, y_valid: Any) -> Tuple[float, float]:
    accuracy = float((probabilities.argmax(axis=1) == y_valid).mean())
    brier = float(np.mean(np.sum((probabilities - np.eye(len(LABELS))[y_valid]) ** 2, axis=1)))
    return accuracy, brier


def _feature_diagnostics(
    np: Any,
    weights: Any,
    train_counts: Any,
    valid_counts: Any,
    accuracy: float,
    baseline: float,
    brier: float,
    no_news_accuracy: float,
    no_news_brier: float,
) -> Dict[str, Any]:
    importance = np.max(np.abs(weights), axis=1)
    ranked = sorted(range(len(FEATURE_NAMES)), key=lambda idx: float(importance[idx]), reverse=True)
    top_features = []
    for index in ranked[:10]:
        row = weights[index]
        positive_index = int(np.argmax(row))
        negative_index = int(np.argmin(row))
        top_features.append({
            "feature": FEATURE_NAMES[index],
            "importance": round(float(importance[index]), 4),
            "supports": LABELS[positive_index],
            "opposes": LABELS[negative_index],
            "weights": {LABELS[i]: round(float(row[i]), 4) for i in range(len(LABELS))},
        })

    top_by_label: Dict[str, List[Dict[str, Any]]] = {}
    for label_index, label in enumerate(LABELS):
        order = sorted(range(len(FEATURE_NAMES)), key=lambda idx: float(weights[idx, label_index]), reverse=True)
        top_by_label[label] = [
            {
                "feature": FEATURE_NAMES[idx],
                "weight": round(float(weights[idx, label_index]), 4),
            }
            for idx in order[:5]
        ]

    raw_group_scores: Dict[str, float] = {}
    for group, names in FEATURE_GROUPS.items():
        indices = [FEATURE_NAMES.index(name) for name in names]
        raw_group_scores[group] = float(sum(float(importance[index]) for index in indices))
    group_total = sum(raw_group_scores.values()) or 1.0
    group_importance = {
        group: round(score / group_total * 100.0, 2)
        for group, score in raw_group_scores.items()
    }

    news_accuracy_delta_pp = round((accuracy - no_news_accuracy) * 100.0, 2)
    news_brier_improvement = round(no_news_brier - brier, 4)
    if news_accuracy_delta_pp >= 1.0 and news_brier_improvement >= 0:
        news_usefulness = "HELPFUL"
    elif news_accuracy_delta_pp <= -1.0 or news_brier_improvement < -0.02:
        news_usefulness = "HARMFUL"
    else:
        news_usefulness = "NEUTRAL_UNPROVEN"

    failed_checks: List[str] = []
    if accuracy < MIN_VALIDATION_ACCURACY:
        failed_checks.append("ACCURACY_BELOW_40_PERCENT")
    if accuracy < baseline + MIN_EDGE_OVER_BASELINE:
        failed_checks.append("NO_EDGE_OVER_BASELINE")
    if brier > MAX_BRIER_SCORE:
        failed_checks.append("PROBABILITY_CALIBRATION_WEAK")
    if min(int(value) for value in train_counts) < MIN_CLASS_TRAIN_SAMPLES:
        failed_checks.append("TRAIN_CLASS_IMBALANCE")
    if min(int(value) for value in valid_counts) < 5:
        failed_checks.append("VALIDATION_CLASS_TOO_SMALL")

    return {
        "activation_gate": {
            "passed": not failed_checks,
            "failed_checks": failed_checks,
            "minimum_accuracy_percent": round(MIN_VALIDATION_ACCURACY * 100, 2),
            "minimum_edge_over_baseline_pp": round(MIN_EDGE_OVER_BASELINE * 100, 2),
            "maximum_brier_score": MAX_BRIER_SCORE,
        },
        "class_distribution": {
            "train": {LABELS[i]: int(train_counts[i]) for i in range(len(LABELS))},
            "validation": {LABELS[i]: int(valid_counts[i]) for i in range(len(LABELS))},
        },
        "top_features": top_features,
        "top_by_label": top_by_label,
        "feature_group_importance_percent": group_importance,
        "news_effect": {
            "usefulness": news_usefulness,
            "validation_accuracy_with_news_percent": round(accuracy * 100.0, 2),
            "validation_accuracy_without_news_percent": round(no_news_accuracy * 100.0, 2),
            "accuracy_delta_percentage_points": news_accuracy_delta_pp,
            "brier_with_news": round(brier, 4),
            "brier_without_news": round(no_news_brier, 4),
            "brier_improvement": news_brier_improvement,
            "meaning": (
                "Positive delta ka matlab news features ne validation improve ki. "
                "Negative delta ka matlab news noise bani."
            ),
        },
    }


def train_horizon(horizon: int = 15) -> Dict[str, Any]:
    ensure_model_schema()
    try:
        import numpy as np
    except Exception as exc:
        _collecting(horizon, 0, f"NUMPY_UNAVAILABLE:{exc}")
        return {"success": False, "status": "COLLECTING", "reason": "NUMPY_UNAVAILABLE"}

    x_rows, y_rows = _training_rows(horizon)
    sample_count = len(y_rows)
    if sample_count < MIN_TRAINING_SAMPLES:
        _collecting(horizon, sample_count)
        return {
            "success": True,
            "status": "COLLECTING",
            "display_status": _display_status("COLLECTING", sample_count),
            "sample_count": sample_count,
            "required": MIN_TRAINING_SAMPLES,
        }

    x = np.asarray(x_rows, dtype=float)
    y = np.asarray(y_rows, dtype=int)
    validation_count = max(MIN_VALIDATION_SAMPLES, int(sample_count * 0.20))
    if sample_count - validation_count < MIN_TRAINING_SAMPLES // 2:
        validation_count = max(30, sample_count // 5)
    split = sample_count - validation_count
    x_train_raw, x_valid_raw = x[:split], x[split:]
    y_train, y_valid = y[:split], y[split:]

    means = x_train_raw.mean(axis=0)
    scales = x_train_raw.std(axis=0)
    scales = np.where(scales < 1e-8, 1.0, scales)
    x_train = (x_train_raw - means) / scales
    x_valid = (x_valid_raw - means) / scales

    weights, bias = _fit_softmax(np, x_train, y_train)
    probabilities = _predict_probabilities(np, x_valid, weights, bias)
    accuracy, brier = _metrics(np, probabilities, y_valid)

    train_counts = np.bincount(y_train, minlength=len(LABELS))
    valid_counts = np.bincount(y_valid, minlength=len(LABELS))
    majority_class = int(np.argmax(train_counts))
    baseline = float((y_valid == majority_class).mean())

    news_indices = [FEATURE_NAMES.index(name) for name in NEWS_FEATURES]
    keep_indices = [index for index in range(len(FEATURE_NAMES)) if index not in news_indices]
    x_train_no_news_raw = x_train_raw[:, keep_indices]
    x_valid_no_news_raw = x_valid_raw[:, keep_indices]
    no_news_means = x_train_no_news_raw.mean(axis=0)
    no_news_scales = x_train_no_news_raw.std(axis=0)
    no_news_scales = np.where(no_news_scales < 1e-8, 1.0, no_news_scales)
    x_train_no_news = (x_train_no_news_raw - no_news_means) / no_news_scales
    x_valid_no_news = (x_valid_no_news_raw - no_news_means) / no_news_scales
    no_news_weights, no_news_bias = _fit_softmax(np, x_train_no_news, y_train)
    no_news_probabilities = _predict_probabilities(np, x_valid_no_news, no_news_weights, no_news_bias)
    no_news_accuracy, no_news_brier = _metrics(np, no_news_probabilities, y_valid)

    diagnostics = _feature_diagnostics(
        np,
        weights,
        train_counts,
        valid_counts,
        accuracy,
        baseline,
        brier,
        no_news_accuracy,
        no_news_brier,
    )
    status = "ACTIVE_SHADOW" if diagnostics["activation_gate"]["passed"] else "VALIDATION_FAILED"

    conn = get_db()
    try:
        conn.execute(
            """UPDATE ai_adaptive_models_v2 SET model_version=?,status=?,trained_at=?,
            sample_count=?,train_count=?,validation_count=?,validation_accuracy=?,baseline_accuracy=?,
            brier_score=?,feature_names_json=?,means_json=?,scales_json=?,weights_json=?,bias_json=?,
            diagnostics_json=?,last_error=NULL WHERE horizon_minutes=?""",
            (
                VERSION,
                status,
                _iso(),
                sample_count,
                len(y_train),
                len(y_valid),
                round(accuracy, 6),
                round(baseline, 6),
                round(brier, 6),
                _dumps(FEATURE_NAMES),
                _dumps(means.tolist()),
                _dumps(scales.tolist()),
                _dumps(weights.tolist()),
                _dumps(bias.tolist()),
                _dumps(diagnostics),
                horizon,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "status": status,
        "display_status": _display_status(status, sample_count),
        "sample_count": sample_count,
        "validation_accuracy_percent": round(accuracy * 100, 2),
        "baseline_accuracy_percent": round(baseline * 100, 2),
        "brier_score": round(brier, 4),
        "diagnostics": diagnostics,
    }


def maybe_train_models(force: bool = False) -> Dict[str, Any]:
    global _last_train
    current = time.monotonic()
    if not force and current - _last_train < TRAIN_INTERVAL_SECONDS:
        return {"success": True, "skipped": True}
    _last_train = current
    output: Dict[str, Any] = {}
    for horizon in (5, 15, 30):
        try:
            output[str(horizon)] = train_horizon(horizon)
        except Exception as exc:
            output[str(horizon)] = {
                "success": False,
                "status": "ERROR",
                "reason": f"{type(exc).__name__}:{str(exc)[:240]}",
            }
    return {"success": True, "models": output}


def predict_adaptive(feature: Mapping[str, Any], horizon: int = 15) -> Dict[str, Any]:
    ensure_model_schema()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM ai_adaptive_models_v2 WHERE horizon_minutes=?",
            (horizon,),
        ).fetchone()
    finally:
        conn.close()

    if row is None or str(row["status"]) != "ACTIVE_SHADOW":
        status = str(row["status"]) if row else "COLLECTING"
        sample_count = int(row["sample_count"] or 0) if row else 0
        diagnostics = _loads(row["diagnostics_json"], {}) if row else {}
        return {
            "available": False,
            "status": status,
            "display_status": _display_status(status, sample_count),
            "status_explanation": _status_explanation(status, sample_count, diagnostics),
            "sample_count": sample_count,
            "required_samples": MIN_TRAINING_SAMPLES,
            "model_version": VERSION,
            "diagnostics": diagnostics,
        }

    try:
        import numpy as np

        vector = np.asarray([[_f(feature.get(name)) for name in FEATURE_NAMES]], dtype=float)
        means = np.asarray(_loads(row["means_json"], []), dtype=float)
        scales = np.asarray(_loads(row["scales_json"], []), dtype=float)
        weights = np.asarray(_loads(row["weights_json"], []), dtype=float)
        bias = np.asarray(_loads(row["bias_json"], []), dtype=float)
        logits = ((vector - means) / scales @ weights + bias)[0]
        logits -= np.max(logits)
        probabilities = np.exp(logits)
        probabilities /= np.sum(probabilities)
        result = {label: int(round(probabilities[index] * 100)) for index, label in enumerate(LABELS)}
        result["NO_TRADE"] += 100 - sum(result.values())
        decision, confidence = max(result.items(), key=lambda item: item[1])
        return {
            "available": True,
            "status": "ACTIVE_SHADOW",
            "display_status": "VALIDATED_SHADOW",
            "model_version": VERSION,
            "horizon_minutes": horizon,
            "decision": decision,
            "confidence": confidence,
            "probabilities": result,
            "sample_count": int(row["sample_count"] or 0),
            "validation_accuracy_percent": round(_f(row["validation_accuracy"]) * 100, 2),
            "brier_score": _f(row["brier_score"]),
            "diagnostics": _loads(row["diagnostics_json"], {}),
        }
    except Exception as exc:
        return {
            "available": False,
            "status": "PREDICTION_ERROR",
            "display_status": "ERROR",
            "reason": f"{type(exc).__name__}:{str(exc)[:200]}",
            "model_version": VERSION,
        }


def model_status() -> Dict[str, Any]:
    ensure_model_schema()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM ai_adaptive_models_v2 ORDER BY horizon_minutes"
        ).fetchall()
        models = []
        for row in rows:
            status = str(row["status"])
            sample_count = int(row["sample_count"] or 0)
            diagnostics = _loads(row["diagnostics_json"], {})
            models.append({
                "horizon_minutes": int(row["horizon_minutes"]),
                "status": status,
                "display_status": _display_status(status, sample_count),
                "status_explanation": _status_explanation(status, sample_count, diagnostics),
                "sample_count": sample_count,
                "train_count": int(row["train_count"] or 0),
                "validation_count": int(row["validation_count"] or 0),
                "validation_accuracy_percent": (
                    round(_f(row["validation_accuracy"]) * 100, 2)
                    if row["validation_accuracy"] is not None
                    else None
                ),
                "baseline_accuracy_percent": (
                    round(_f(row["baseline_accuracy"]) * 100, 2)
                    if row["baseline_accuracy"] is not None
                    else None
                ),
                "brier_score": row["brier_score"],
                "trained_at": row["trained_at"],
                "last_error": row["last_error"],
                "diagnostics": diagnostics,
            })
        return {
            "success": True,
            "model_version": VERSION,
            "minimum_training_samples": MIN_TRAINING_SAMPLES,
            "activation_rules": {
                "minimum_validation_accuracy_percent": round(MIN_VALIDATION_ACCURACY * 100, 2),
                "minimum_edge_over_baseline_pp": round(MIN_EDGE_OVER_BASELINE * 100, 2),
                "maximum_brier_score": MAX_BRIER_SCORE,
                "minimum_class_train_samples": MIN_CLASS_TRAIN_SAMPLES,
            },
            "models": models,
            "trade_blocking": False,
            "order_execution": False,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    ensure_model_schema()
    print(json.dumps(maybe_train_models(force=True), indent=2))
