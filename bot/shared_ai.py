"""Shared Option King AI prediction core.

This module is intentionally broker-agnostic. Personal bot and SaaS send the
same normalized market snapshot and receive CE / PE / NO_TRADE probabilities.
It never logs in to a broker and never places, modifies, or closes an order.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List


MODEL_VERSION = "railway-shared-0.1.0"
MIN_CONFIDENCE = 75
MAX_FEED_AGE_MS = 130_000
MAX_SPREAD_PERCENT = 1.2
MAX_DAILY_LOSS_PERCENT = 1.5
MAX_CONSECUTIVE_LOSSES = 2
MIN_ADX = 18.0
STRONG_ADX = 25.0
MIN_VOLUME_RATIO = 0.9
STRONG_VOLUME_RATIO = 1.2
MAX_ATR_PERCENT = 1.8
MAX_VWAP_DISTANCE_PERCENT = 0.65


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return default


def _boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "ok", "confirmed", "live"}


def _direction(value: Any) -> int:
    text = str(value or "").strip().upper()
    if not text:
        return 0
    if "CE" in text or text in {"BUY", "BULLISH", "UP", "UPTREND", "LONG"}:
        return 1
    if "PE" in text or text in {"SELL", "BEARISH", "DOWN", "DOWNTREND", "SHORT"}:
        return -1
    return 0


def _percent_distance(value: float, base: float) -> float:
    if not base:
        return 0.0
    return ((value - base) / abs(base)) * 100.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalize_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    price = _number(payload.get("price", payload.get("ltp", payload.get("close"))), 0.0)
    ema_fast = _number(
        payload.get("ema_fast", payload.get("ema20", payload.get("ema9"))),
        price,
    )
    ema_slow = _number(
        payload.get("ema_slow", payload.get("ema50", payload.get("ema21"))),
        price,
    )
    vwap = _number(payload.get("vwap"), price)
    atr = max(0.0, _number(payload.get("atr"), 0.0))
    atr_percent = payload.get("atr_percent")
    if atr_percent is None:
        atr_percent = (atr / price * 100.0) if price > 0 else 0.0

    strategy_score = _clamp(
        _number(payload.get("strategy_score", payload.get("score")), 0.0),
        0.0,
        100.0,
    )
    min_strategy_score = _clamp(
        _number(payload.get("min_strategy_score", payload.get("min_score")), 75.0),
        1.0,
        100.0,
    )

    signal_direction = _direction(
        payload.get(
            "signal_direction",
            payload.get("trade_side", payload.get("side", payload.get("signal"))),
        )
    )

    return {
        "source": str(payload.get("source") or "UNKNOWN"),
        "symbol": str(payload.get("symbol") or payload.get("underlying") or "NIFTY").upper(),
        "feed_connected": _boolean(payload.get("feed_connected"), False),
        "feed_age_ms": max(0, int(_number(payload.get("feed_age_ms"), 999999))),
        "market_open": _boolean(payload.get("market_open"), True),
        "price": price,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "vwap": vwap,
        "ema_direction": 1 if ema_fast > ema_slow else -1 if ema_fast < ema_slow else 0,
        "price_vs_vwap_percent": _percent_distance(price, vwap),
        "adx": _clamp(_number(payload.get("adx"), 0.0), 0.0, 100.0),
        "rsi": _clamp(_number(payload.get("rsi"), 50.0), 0.0, 100.0),
        "atr_percent": max(0.0, _number(atr_percent, 0.0)),
        "volume_ratio": max(0.0, _number(payload.get("volume_ratio"), 0.0)),
        "spread_percent": max(0.0, _number(payload.get("spread_percent"), 0.0)),
        "signal_direction": signal_direction,
        "supertrend_direction": _direction(
            payload.get("supertrend_direction", payload.get("supertrend_dir", payload.get("supertrend")))
        ),
        "structure_direction": _direction(
            payload.get("structure_direction", payload.get("market_structure"))
        ),
        "mtf_direction": _direction(payload.get("mtf_direction", payload.get("mtf_trend"))),
        "mtf_confirmed": _boolean(payload.get("mtf_confirmed"), False),
        "strategy_score": strategy_score,
        "min_strategy_score": min_strategy_score,
        "server_trade_allowed": _boolean(
            payload.get("server_trade_allowed", payload.get("trade_allowed")),
            False,
        ),
        "market_regime": str(payload.get("market_regime") or payload.get("regime") or "").upper(),
        "daily_loss_percent": max(0.0, _number(payload.get("daily_loss_percent"), 0.0)),
        "consecutive_losses": max(0, int(_number(payload.get("consecutive_losses"), 0))),
        "has_open_position": _boolean(payload.get("has_open_position"), False),
    }


def _risk_gate(features: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if not features["market_open"]:
        reasons.append("MARKET_CLOSED")
    if not features["feed_connected"]:
        reasons.append("FEED_DISCONNECTED")
    if features["feed_age_ms"] > MAX_FEED_AGE_MS:
        reasons.append("STALE_DATA")
    if features["price"] <= 0:
        reasons.append("INVALID_PRICE")
    if features["spread_percent"] > MAX_SPREAD_PERCENT:
        reasons.append("SPREAD_TOO_HIGH")
    if features["daily_loss_percent"] >= MAX_DAILY_LOSS_PERCENT:
        reasons.append("DAILY_LOSS_LIMIT")
    if features["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        reasons.append("CONSECUTIVE_LOSS_LIMIT")
    if features["atr_percent"] > MAX_ATR_PERCENT:
        reasons.append("VOLATILITY_TOO_HIGH")
    if abs(features["price_vs_vwap_percent"]) > MAX_VWAP_DISTANCE_PERCENT:
        reasons.append("PRICE_OVEREXTENDED")
    if features["has_open_position"]:
        reasons.append("POSITION_ALREADY_OPEN")
    return reasons


def _add_direction(scores: Dict[str, float], direction: int, points: float) -> None:
    if direction > 0:
        scores["CE"] += points
    elif direction < 0:
        scores["PE"] += points


def _softmax(scores: Dict[str, float]) -> Dict[str, int]:
    temperature = 22.0
    maximum = max(scores.values())
    raw = {key: math.exp((value - maximum) / temperature) for key, value in scores.items()}
    total = sum(raw.values()) or 1.0

    model_weight = 0.90
    neutral_prior = (1.0 - model_weight) / 3.0
    blended = {
        key: ((raw[key] / total) * model_weight + neutral_prior) * 100.0
        for key in raw
    }

    ce = round(blended["CE"])
    pe = round(blended["PE"])
    no_trade = max(0, 100 - ce - pe)
    return {"CE": ce, "PE": pe, "NO_TRADE": no_trade}


def predict(payload: Dict[str, Any]) -> Dict[str, Any]:
    features = normalize_snapshot(payload)
    gate_reasons = _risk_gate(features)
    if gate_reasons:
        return {
            "success": True,
            "model_version": MODEL_VERSION,
            "decision": "NO_TRADE",
            "confidence": 100,
            "probabilities": {"CE": 0, "PE": 0, "NO_TRADE": 100},
            "risk_allowed": False,
            "reasons": gate_reasons,
            "features_used": features,
        }

    scores: Dict[str, float] = {"CE": 20.0, "PE": 20.0, "NO_TRADE": 30.0}
    reasons: List[str] = []

    _add_direction(scores, features["signal_direction"], 16)
    _add_direction(scores, features["ema_direction"], 12)
    _add_direction(scores, features["supertrend_direction"], 12)
    _add_direction(scores, features["structure_direction"], 10)
    _add_direction(scores, features["mtf_direction"], 8)

    if features["price_vs_vwap_percent"] > 0:
        scores["CE"] += 10
    elif features["price_vs_vwap_percent"] < 0:
        scores["PE"] += 10

    if 52 <= features["rsi"] < 75:
        scores["CE"] += 7
    elif 25 < features["rsi"] <= 48:
        scores["PE"] += 7

    score_ratio = features["strategy_score"] / max(1.0, features["min_strategy_score"])
    if features["signal_direction"] and features["server_trade_allowed"] and score_ratio >= 1.0:
        _add_direction(scores, features["signal_direction"], 24)
    elif features["signal_direction"] and score_ratio >= 0.90:
        _add_direction(scores, features["signal_direction"], 12)
        scores["NO_TRADE"] += 6
        reasons.append("STRATEGY_SCORE_NEAR_THRESHOLD")
    elif features["strategy_score"] > 0:
        scores["NO_TRADE"] += 18
        reasons.append("STRATEGY_SCORE_BELOW_THRESHOLD")

    if features["adx"] >= STRONG_ADX:
        direction = features["ema_direction"] or features["signal_direction"]
        _add_direction(scores, direction, 9)
    elif features["adx"] < MIN_ADX:
        scores["NO_TRADE"] += 24
        reasons.append("ADX_WEAK")

    if features["volume_ratio"] >= STRONG_VOLUME_RATIO:
        direction = features["signal_direction"] or features["ema_direction"]
        _add_direction(scores, direction, 8)
    elif features["volume_ratio"] < MIN_VOLUME_RATIO:
        scores["NO_TRADE"] += 16
        reasons.append("VOLUME_WEAK")

    if features["mtf_confirmed"]:
        _add_direction(scores, features["signal_direction"] or features["ema_direction"], 10)
    else:
        scores["NO_TRADE"] += 15
        reasons.append("MTF_NOT_CONFIRMED")

    if "SIDEWAYS" in features["market_regime"]:
        scores["NO_TRADE"] += 25
        reasons.append("SIDEWAYS_MARKET")

    directions = [
        value
        for value in (
            features["signal_direction"],
            features["ema_direction"],
            features["supertrend_direction"],
            features["structure_direction"],
            features["mtf_direction"],
        )
        if value
    ]
    if 1 in directions and -1 in directions:
        scores["NO_TRADE"] += 24
        reasons.append("DIRECTION_CONFLICT")

    scores = {key: _clamp(value, 0.0, 100.0) for key, value in scores.items()}
    probabilities = _softmax(scores)
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
    decision, confidence = ranked[0]

    if decision != "NO_TRADE" and confidence < MIN_CONFIDENCE:
        reasons.append("CONFIDENCE_BELOW_MINIMUM")
        decision = "NO_TRADE"
        confidence = max(probabilities["NO_TRADE"], 100 - ranked[0][1])

    return {
        "success": True,
        "model_version": MODEL_VERSION,
        "decision": decision,
        "confidence": int(confidence),
        "probabilities": probabilities,
        "risk_allowed": True,
        "reasons": reasons,
        "features_used": features,
    }
