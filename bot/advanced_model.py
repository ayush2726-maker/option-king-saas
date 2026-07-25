"""Feature fusion, confidence calibration and automatic walk-forward model."""
from __future__ import annotations

import json
import math
import statistics
from typing import Any, Dict, Mapping, Sequence

from bot.advanced_broker_data import clamp, direction, num

FEATURE_NAMES = (
    "base_ce", "base_pe", "base_no_trade", "strategy_score_ratio",
    "adx", "volume_ratio", "price_vwap_percent",
    "news_ce", "news_pe", "news_strength", "news_risk",
    "global_ce", "global_pe", "global_strength", "global_risk",
    "option_ce", "option_pe", "option_strength", "pcr",
    "max_pain_distance_percent", "change_oi_bias", "iv_skew",
    "depth_imbalance", "spread_percent", "data_quality",
)
LABELS = ("CE", "PE", "NO_TRADE")
MIN_MODEL_SAMPLES = 300


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value, default):
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def feature_vector(market, base, news, global_data, options):
    probs = base.get("probabilities") or {}
    price = num(market.get("price"))
    vwap = num(market.get("vwap"), price)
    news_bias = direction(news.get("news_bias"))
    global_bias = direction(global_data.get("global_bias"))
    option_bias = direction(options.get("option_bias"))
    return {
        "base_ce": num(probs.get("CE")) / 100,
        "base_pe": num(probs.get("PE")) / 100,
        "base_no_trade": num(probs.get("NO_TRADE")) / 100,
        "strategy_score_ratio": num(market.get("strategy_score")) / max(1, num(market.get("min_strategy_score"), 82)),
        "adx": num(market.get("adx")) / 100,
        "volume_ratio": num(market.get("volume_ratio")),
        "price_vwap_percent": (price - vwap) / max(abs(vwap), 1) * 100 if price and vwap else 0,
        "news_ce": 1 if news_bias == "CE" else 0,
        "news_pe": 1 if news_bias == "PE" else 0,
        "news_strength": num(news.get("news_strength")) / 100,
        "news_risk": num(news.get("news_risk_score")) / 100,
        "global_ce": 1 if global_bias == "CE" else 0,
        "global_pe": 1 if global_bias == "PE" else 0,
        "global_strength": num(global_data.get("global_strength")) / 100,
        "global_risk": num(global_data.get("global_risk_score")) / 100,
        "option_ce": 1 if option_bias == "CE" else 0,
        "option_pe": 1 if option_bias == "PE" else 0,
        "option_strength": num(options.get("option_strength")) / 100,
        "pcr": num(options.get("pcr"), 1),
        "max_pain_distance_percent": num(options.get("max_pain_distance_percent")),
        "change_oi_bias": (
            num(options.get("put_change_oi")) - num(options.get("call_change_oi"))
        ) / max(1, abs(num(options.get("put_oi"))) + abs(num(options.get("call_oi")))),
        "iv_skew": num(options.get("iv_skew")) / 100,
        "depth_imbalance": num(options.get("depth_imbalance")),
        "spread_percent": num(options.get("average_spread_percent")) / 100,
        "data_quality": num(options.get("data_quality_score")) / 100,
    }


def softmax(values):
    high = max(values)
    raw = [math.exp(value - high) for value in values]
    total = sum(raw) or 1
    return [value / total for value in raw]


def model_predict(features, registry):
    model = registry.get("model") or {}
    if not registry.get("active") or not model:
        return {"active": False, "probabilities": {}, "confidence": 0}
    means, stds = model.get("means") or {}, model.get("stds") or {}
    vector = [
        (num(features.get(name)) - num(means.get(name))) / max(1e-6, num(stds.get(name), 1))
        for name in FEATURE_NAMES
    ]
    x = [1] + vector
    probabilities_raw = softmax([
        sum(num(weight) * value for weight, value in zip(row, x))
        for row in model.get("weights") or []
    ])
    if len(probabilities_raw) != 3:
        return {"active": False, "probabilities": {}, "confidence": 0}
    probabilities = {
        label: int(round(probability * 100))
        for label, probability in zip(LABELS, probabilities_raw)
    }
    probabilities["NO_TRADE"] += 100 - sum(probabilities.values())
    decision, raw_confidence = max(probabilities.items(), key=lambda item: item[1])
    bucket = str(min(9, max(0, raw_confidence // 10)))
    calibrated = num(
        ((registry.get("calibration") or {}).get(bucket) or {}).get("accuracy_percent"),
        raw_confidence,
    )
    return {
        "active": True, "decision": decision,
        "confidence": int(round(calibrated)),
        "raw_confidence": raw_confidence,
        "probabilities": probabilities,
        "sample_count": int(num(registry.get("sample_count"))),
        "model_version": registry.get("model_version"),
    }


def fuse(market, base, news, global_data, options, registry):
    base_probs = base.get("probabilities") or {}
    scores = {
        "CE": num(base_probs.get("CE")),
        "PE": num(base_probs.get("PE")),
        "NO_TRADE": num(base_probs.get("NO_TRADE"), 100),
    }
    reasons = []
    option_bias = direction(options.get("option_bias"))
    quality = num(options.get("data_quality_score"))
    if option_bias in {"CE", "PE"}:
        opposite = "PE" if option_bias == "CE" else "CE"
        shift = clamp(8 + num(options.get("option_strength")) * .28, 8, 36) * max(.35, quality / 100)
        scores[option_bias] += shift
        scores[opposite] -= shift * .55
        reasons.append("OPTION_CHAIN_DIRECTION")
    else:
        scores["NO_TRADE"] += 8
        reasons.append("OPTION_CHAIN_NEUTRAL")
    spread = num(options.get("average_spread_percent"))
    if spread >= 2:
        scores["NO_TRADE"] += min(30, spread * 5)
        reasons.append("OPTION_SPREAD_HIGH")
    if quality < 45:
        scores["NO_TRADE"] += 18
        reasons.append("OPTION_DATA_QUALITY_LOW")

    news_bias = direction(news.get("news_bias"))
    if news_bias in {"CE", "PE"} and news.get("fresh"):
        scores[news_bias] += clamp(num(news.get("news_strength")) * .18, 4, 22)
        reasons.append("NEWS_DIRECTION")
    if num(news.get("news_risk_score")) >= 70:
        scores["NO_TRADE"] += clamp((num(news.get("news_risk_score")) - 60) * .7, 8, 28)
        reasons.append("NEWS_EVENT_RISK")

    global_bias = direction(global_data.get("global_bias"))
    if global_bias in {"CE", "PE"}:
        scores[global_bias] += clamp(num(global_data.get("global_strength")) * .16, 3, 18)
        reasons.append("GLOBAL_MARKET_DIRECTION")
    if num(global_data.get("global_risk_score")) >= 65:
        scores["NO_TRADE"] += clamp((num(global_data.get("global_risk_score")) - 55) * .6, 6, 24)
        reasons.append("GLOBAL_MARKET_RISK")

    features = feature_vector(market, base, news, global_data, options)
    trained = model_predict(features, registry)
    if trained.get("active"):
        for label, probability in trained["probabilities"].items():
            scores[label] += probability * .35
        reasons.append("CALIBRATED_MODEL_OVERLAY")
    else:
        reasons.append("MODEL_COLLECTING_DATA")

    scores = {name: max(0, value) for name, value in scores.items()}
    total = sum(scores.values()) or 1
    probabilities = {name: int(round(value / total * 100)) for name, value in scores.items()}
    probabilities["NO_TRADE"] += 100 - sum(probabilities.values())
    decision, confidence = max(probabilities.items(), key=lambda item: item[1])
    if decision != "NO_TRADE" and confidence < 55:
        decision, confidence = "NO_TRADE", probabilities["NO_TRADE"]
        reasons.append("ADVANCED_CONFIDENCE_LOW")
    return {
        "success": True, "decision": decision, "confidence": confidence,
        "probabilities": probabilities, "features": features,
        "calibrated_model": trained, "reasons": reasons,
        "mode": "BROKER_NEUTRAL_ADVANCED_SHADOW_ONLY",
        "trade_blocking": False, "order_execution": False,
    }


def train(rows):
    samples = []
    for row in rows:
        features = loads(row["feature_json"], {})
        label = str(row["best_label"] or "NO_TRADE")
        if label in LABELS:
            samples.append((
                [num(features.get(name)) for name in FEATURE_NAMES],
                LABELS.index(label), row,
            ))
    if len(samples) < MIN_MODEL_SAMPLES:
        return {"success": False, "reason": "INSUFFICIENT_SAMPLES", "sample_count": len(samples)}
    split = max(1, int(len(samples) * .8))
    training, validation = samples[:split], samples[split:]
    means, stds = [], []
    for index in range(len(FEATURE_NAMES)):
        values = [sample[0][index] for sample in training]
        means.append(statistics.mean(values))
        std = statistics.pstdev(values) if len(values) > 1 else 1
        stds.append(std if std > 1e-6 else 1)
    def normalize(vector):
        return [(value - mean) / std for value, mean, std in zip(vector, means, stds)]
    weights = [[0.0] * (len(FEATURE_NAMES) + 1) for _ in LABELS]
    counts = [sum(1 for _, label, _ in training if label == index) for index in range(3)]
    class_weights = [len(training) / max(1, 3 * count) for count in counts]
    for epoch in range(260):
        rate = .08 / (1 + epoch * .012)
        gradients = [[0.0] * len(weights[0]) for _ in LABELS]
        for vector, target, _ in training:
            x = [1] + normalize(vector)
            probs = softmax([sum(w * v for w, v in zip(row, x)) for row in weights])
            for cls in range(3):
                error = (probs[cls] - (1 if cls == target else 0)) * class_weights[target]
                for index, value in enumerate(x):
                    gradients[cls][index] += error * value
        for cls in range(3):
            for index in range(len(weights[cls])):
                regularization = .001 * weights[cls][index] if index else 0
                weights[cls][index] -= rate * (
                    gradients[cls][index] / max(1, len(training)) + regularization
                )
    correct, net_utility, confidences = 0, 0.0, []
    label_counts = [sum(1 for _, label, _ in validation if label == index) for index in range(3)]
    for vector, target, row in validation:
        x = [1] + normalize(vector)
        probs = softmax([sum(w * v for w, v in zip(model_row, x)) for model_row in weights])
        predicted = max(range(3), key=lambda index: probs[index])
        correct_flag = predicted == target
        correct += int(correct_flag)
        confidences.append((int(round(probs[predicted] * 100)), correct_flag))
        chosen = LABELS[predicted]
        net_utility += (
            num(row["ce_net_pnl"]) if chosen == "CE"
            else num(row["pe_net_pnl"]) if chosen == "PE"
            else 0
        )
    accuracy = correct / max(1, len(validation))
    majority = max(label_counts, default=0) / max(1, len(validation))
    calibration: Dict[str, Dict[str, Any]] = {}
    for confidence, correct_flag in confidences:
        bucket = str(min(9, max(0, confidence // 10)))
        item = calibration.setdefault(bucket, {"count": 0, "correct": 0})
        item["count"] += 1
        item["correct"] += int(correct_flag)
    for item in calibration.values():
        item["accuracy_percent"] = round(item["correct"] / max(1, item["count"]) * 100, 2)
    active = len(validation) >= 50 and accuracy >= max(.40, majority + .02) and net_utility > 0
    return {
        "success": True, "active": active, "sample_count": len(samples),
        "validation_count": len(validation),
        "validation_accuracy": round(accuracy * 100, 2),
        "majority_baseline_accuracy": round(majority * 100, 2),
        "validation_net_utility": round(net_utility, 2),
        "model": {
            "feature_names": list(FEATURE_NAMES),
            "labels": list(LABELS),
            "means": dict(zip(FEATURE_NAMES, means)),
            "stds": dict(zip(FEATURE_NAMES, stds)),
            "weights": weights,
        },
        "calibration": calibration,
    }
