"""Leakage-safe adaptive classifier for exact option outcomes.

The model stays COLLECTING until at least 300 chronological samples exist.
It validates on the newest holdout and remains shadow-only.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from database import get_db

VERSION = "OKAI-ADAPTIVE-SOFTMAX-V2"
MIN_TRAINING_SAMPLES = 300
MIN_VALIDATION_SAMPLES = 60
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
_last_train = 0.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _loads(value, default):
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def ensure_model_schema():
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
          bias_json TEXT NOT NULL DEFAULT '[]',last_error TEXT);
        """)
        for horizon in (5, 15, 30):
            conn.execute("""INSERT OR IGNORE INTO ai_adaptive_models_v2(
            horizon_minutes,model_version,status,feature_names_json)
            VALUES(?,?,'COLLECTING',?)""", (horizon, VERSION, _dumps(FEATURE_NAMES)))
        conn.commit()
    finally:
        conn.close()


def feature_vector(*, market: Mapping[str, Any], base: Mapping[str, Any], option: Mapping[str, Any], news: Mapping[str, Any], global_market: Mapping[str, Any]) -> Dict[str, float]:
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
        rows = conn.execute("""SELECT s.feature_json,o.best_label
        FROM ai_advanced_v2_snapshots s JOIN ai_advanced_v2_contract_outcomes o
        ON o.decision_id=s.id WHERE o.horizon_minutes=?
        AND o.best_label IN('CE','PE','NO_TRADE')
        ORDER BY datetime(s.created_at),s.rowid""", (horizon,)).fetchall()
    finally:
        conn.close()
    label_index = {label: index for index, label in enumerate(LABELS)}
    x, y = [], []
    for row in rows:
        feature = _loads(row["feature_json"], {})
        x.append([_f(feature.get(name)) for name in FEATURE_NAMES])
        y.append(label_index[str(row["best_label"])])
    return x, y


def _collecting(horizon, count, error=None):
    conn = get_db()
    try:
        conn.execute("""UPDATE ai_adaptive_models_v2 SET model_version=?,status='COLLECTING',
        sample_count=?,last_error=? WHERE horizon_minutes=?""",
        (VERSION, count, str(error)[:300] if error else None, horizon))
        conn.commit()
    finally:
        conn.close()


def train_horizon(horizon=15):
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
        return {"success": True, "status": "COLLECTING", "sample_count": sample_count, "required": MIN_TRAINING_SAMPLES}
    x = np.asarray(x_rows, dtype=float)
    y = np.asarray(y_rows, dtype=int)
    validation_count = max(MIN_VALIDATION_SAMPLES, int(sample_count * 0.20))
    if sample_count - validation_count < MIN_TRAINING_SAMPLES // 2:
        validation_count = max(30, sample_count // 5)
    split = sample_count - validation_count
    x_train, x_valid = x[:split], x[split:]
    y_train, y_valid = y[:split], y[split:]
    means = x_train.mean(axis=0)
    scales = x_train.std(axis=0)
    scales = np.where(scales < 1e-8, 1.0, scales)
    x_train = (x_train - means) / scales
    x_valid = (x_valid - means) / scales
    classes = len(LABELS)
    weights = np.zeros((len(FEATURE_NAMES), classes))
    bias = np.zeros(classes)
    target = np.eye(classes)[y_train]
    for _ in range(700):
        logits = x_train @ weights + bias
        logits -= logits.max(axis=1, keepdims=True)
        exp = np.exp(logits)
        probabilities = exp / exp.sum(axis=1, keepdims=True)
        error = probabilities - target
        weights -= 0.045 * (x_train.T @ error / len(x_train) + 0.003 * weights)
        bias -= 0.045 * error.mean(axis=0)
    logits = x_valid @ weights + bias
    logits -= logits.max(axis=1, keepdims=True)
    exp = np.exp(logits)
    probabilities = exp / exp.sum(axis=1, keepdims=True)
    accuracy = float((probabilities.argmax(axis=1) == y_valid).mean())
    counts = np.bincount(y_train, minlength=classes)
    baseline = float(counts.max() / counts.sum())
    brier = float(np.mean(np.sum((probabilities - np.eye(classes)[y_valid]) ** 2, axis=1)))
    status = "ACTIVE_SHADOW" if accuracy >= baseline and brier <= 0.72 else "VALIDATION_FAILED"
    conn = get_db()
    try:
        conn.execute("""UPDATE ai_adaptive_models_v2 SET model_version=?,status=?,trained_at=?,
        sample_count=?,train_count=?,validation_count=?,validation_accuracy=?,baseline_accuracy=?,
        brier_score=?,feature_names_json=?,means_json=?,scales_json=?,weights_json=?,bias_json=?,last_error=NULL
        WHERE horizon_minutes=?""", (VERSION, status, _iso(), sample_count, len(y_train), len(y_valid),
        round(accuracy, 6), round(baseline, 6), round(brier, 6), _dumps(FEATURE_NAMES),
        _dumps(means.tolist()), _dumps(scales.tolist()), _dumps(weights.tolist()), _dumps(bias.tolist()), horizon))
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "status": status, "sample_count": sample_count,
            "validation_accuracy_percent": round(accuracy * 100, 2),
            "baseline_accuracy_percent": round(baseline * 100, 2), "brier_score": round(brier, 4)}


def maybe_train_models(force=False):
    global _last_train
    current = time.monotonic()
    if not force and current - _last_train < TRAIN_INTERVAL_SECONDS:
        return {"success": True, "skipped": True}
    _last_train = current
    output = {}
    for horizon in (5, 15, 30):
        try:
            output[str(horizon)] = train_horizon(horizon)
        except Exception as exc:
            output[str(horizon)] = {"success": False, "status": "ERROR", "reason": f"{type(exc).__name__}:{str(exc)[:240]}"}
    return {"success": True, "models": output}


def predict_adaptive(feature: Mapping[str, Any], horizon=15):
    ensure_model_schema()
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM ai_adaptive_models_v2 WHERE horizon_minutes=?", (horizon,)).fetchone()
    finally:
        conn.close()
    if row is None or str(row["status"]) != "ACTIVE_SHADOW":
        return {"available": False, "status": str(row["status"]) if row else "COLLECTING",
                "sample_count": int(row["sample_count"] or 0) if row else 0,
                "required_samples": MIN_TRAINING_SAMPLES, "model_version": VERSION}
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
        return {"available": True, "status": "ACTIVE_SHADOW", "model_version": VERSION,
                "horizon_minutes": horizon, "decision": decision, "confidence": confidence,
                "probabilities": result, "sample_count": int(row["sample_count"] or 0),
                "validation_accuracy_percent": round(_f(row["validation_accuracy"]) * 100, 2),
                "brier_score": _f(row["brier_score"])}
    except Exception as exc:
        return {"available": False, "status": "PREDICTION_ERROR",
                "reason": f"{type(exc).__name__}:{str(exc)[:200]}", "model_version": VERSION}


def model_status():
    ensure_model_schema()
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM ai_adaptive_models_v2 ORDER BY horizon_minutes").fetchall()
        return {"success": True, "model_version": VERSION,
                "minimum_training_samples": MIN_TRAINING_SAMPLES,
                "models": [{"horizon_minutes": int(row["horizon_minutes"]),
                "status": str(row["status"]), "sample_count": int(row["sample_count"] or 0),
                "validation_accuracy_percent": round(_f(row["validation_accuracy"]) * 100, 2) if row["validation_accuracy"] is not None else None,
                "baseline_accuracy_percent": round(_f(row["baseline_accuracy"]) * 100, 2) if row["baseline_accuracy"] is not None else None,
                "brier_score": row["brier_score"], "trained_at": row["trained_at"],
                "last_error": row["last_error"]} for row in rows],
                "trade_blocking": False, "order_execution": False}
    finally:
        conn.close()
