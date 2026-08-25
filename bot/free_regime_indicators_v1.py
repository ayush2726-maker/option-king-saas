"""Free broker-candle regime features for the adaptive shadow model.

This module computes Choppiness Index and Squeeze Momentum from completed
OHLC candles already fetched by the bot.  It does not call TradingView or a
paid data provider and has no entry, blocking, sizing, exit, or order authority.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Sequence, Tuple


VERSION = "OKAI-FREE-REGIME-INDICATORS-V1"
CHOP_LENGTH = 14
SQUEEZE_LENGTH = 20
FEATURES = (
    "free_indicator_available",
    "choppiness_index",
    "choppiness_trending",
    "choppiness_sideways",
    "squeeze_on",
    "squeeze_release",
    "squeeze_momentum",
    "squeeze_direction_ce",
    "squeeze_direction_pe",
    "squeeze_momentum_rising",
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _clip(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, _f(value)))


def _neutral() -> Dict[str, float]:
    return {name: 0.0 for name in FEATURES}


def completed_chart_candles(state: Mapping[str, Any], limit: int = 80) -> List[Dict[str, Any]]:
    """Copy valid OHLC rows while dropping the fetcher's forming last candle."""
    candles = state.get("chart_candles") or []
    if not isinstance(candles, list) or len(candles) < 2:
        return []
    output: List[Dict[str, Any]] = []
    for row in candles[:-1]:
        if not isinstance(row, Mapping):
            continue
        values = {key: _f(row.get(key)) for key in ("open", "high", "low", "close")}
        if min(values.values()) <= 0.0 or values["high"] < values["low"]:
            continue
        output.append({"time": str(row.get("time") or ""), **values})
    return output[-max(1, min(120, int(limit))) :]


def _candles(market: Mapping[str, Any]) -> List[Tuple[float, float, float, float]]:
    # Do not silently accept chart_candles: its newest item can be unfinished.
    raw = market.get("completed_candles") or []
    output: List[Tuple[float, float, float, float]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        open_price = _f(row.get("open"))
        high = _f(row.get("high"))
        low = _f(row.get("low"))
        close = _f(row.get("close"))
        if min(open_price, high, low, close) <= 0.0 or high < low:
            continue
        output.append((open_price, high, low, close))
    return output[-80:]


def _true_ranges(candles: Sequence[Tuple[float, float, float, float]]) -> List[float]:
    output: List[float] = []
    for index, (_, high, low, _) in enumerate(candles):
        if index == 0:
            output.append(high - low)
            continue
        previous_close = candles[index - 1][3]
        output.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return output


def _choppiness(
    candles: Sequence[Tuple[float, float, float, float]],
    true_ranges: Sequence[float],
    length: int = CHOP_LENGTH,
) -> float:
    window = candles[-length:]
    price_range = max(row[1] for row in window) - min(row[2] for row in window)
    if price_range <= 1e-12:
        return 100.0
    ratio = max(1.0, sum(true_ranges[-length:]) / price_range)
    return _clip(100.0 * math.log10(ratio) / math.log10(length), 0.0, 100.0)


def _bands(
    candles: Sequence[Tuple[float, float, float, float]],
    true_ranges: Sequence[float],
    end: int,
    length: int = SQUEEZE_LENGTH,
) -> Tuple[float, float, float, float]:
    start = end - length + 1
    closes = [row[3] for row in candles[start : end + 1]]
    middle = sum(closes) / length
    variance = sum((value - middle) ** 2 for value in closes) / length
    deviation = math.sqrt(max(0.0, variance))
    atr = sum(true_ranges[start : end + 1]) / length
    return (
        middle - 2.0 * deviation,
        middle + 2.0 * deviation,
        middle - 1.5 * atr,
        middle + 1.5 * atr,
    )


def _squeeze_on(bands: Tuple[float, float, float, float]) -> bool:
    lower_bb, upper_bb, lower_kc, upper_kc = bands
    return lower_bb > lower_kc and upper_bb < upper_kc


def _linear_regression_last(values: Sequence[float]) -> float:
    count = len(values)
    x_mean = (count - 1.0) / 2.0
    y_mean = sum(values) / count
    denominator = sum((index - x_mean) ** 2 for index in range(count))
    if denominator <= 1e-12:
        return y_mean
    slope = sum(
        (index - x_mean) * (value - y_mean)
        for index, value in enumerate(values)
    ) / denominator
    intercept = y_mean - slope * x_mean
    return intercept + slope * (count - 1.0)


def _momentum_series(
    candles: Sequence[Tuple[float, float, float, float]],
    length: int = SQUEEZE_LENGTH,
) -> List[float]:
    values: List[float] = []
    for end in range(length - 1, len(candles)):
        window = candles[end - length + 1 : end + 1]
        closes = [row[3] for row in window]
        midrange = (max(row[1] for row in window) + min(row[2] for row in window)) / 2.0
        baseline = (midrange + sum(closes) / length) / 2.0
        values.append(candles[end][3] - baseline)
    return values


def free_regime_features(market: Mapping[str, Any]) -> Dict[str, float]:
    """Return bounded decision-time features; unavailable data stays neutral."""
    candles = _candles(market)
    required = SQUEEZE_LENGTH * 2
    if len(candles) < required:
        return _neutral()

    true_ranges = _true_ranges(candles)
    chop = _choppiness(candles, true_ranges)
    width = 61.8 - 38.2
    trending = _clip((61.8 - chop) / width)
    sideways = _clip((chop - 38.2) / width)

    latest_bands = _bands(candles, true_ranges, len(candles) - 1)
    previous_bands = _bands(candles, true_ranges, len(candles) - 2)
    squeeze_now = _squeeze_on(latest_bands)
    squeeze_before = _squeeze_on(previous_bands)

    source = _momentum_series(candles)
    current_raw = _linear_regression_last(source[-SQUEEZE_LENGTH:])
    previous_raw = _linear_regression_last(source[-SQUEEZE_LENGTH - 1 : -1])
    atr = sum(true_ranges[-SQUEEZE_LENGTH:]) / SQUEEZE_LENGTH
    scale = max(atr, candles[-1][3] * 0.001, 1e-9)
    momentum = _clip(current_raw / scale, -1.0, 1.0)

    return {
        "free_indicator_available": 1.0,
        "choppiness_index": chop / 100.0,
        "choppiness_trending": trending,
        "choppiness_sideways": sideways,
        "squeeze_on": 1.0 if squeeze_now else 0.0,
        "squeeze_release": 1.0 if squeeze_before and not squeeze_now else 0.0,
        "squeeze_momentum": momentum,
        "squeeze_direction_ce": 1.0 if current_raw > 0.0 else 0.0,
        "squeeze_direction_pe": 1.0 if current_raw < 0.0 else 0.0,
        "squeeze_momentum_rising": 1.0 if current_raw > previous_raw else 0.0,
    }


def free_regime_status() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "source": "BROKER_COMPLETED_OHLC",
        "paid_service_required": False,
        "tradingview_required": False,
        "mode": "TRAINING_ONLY_SHADOW",
        "decision_authority": "BASELINE_STRATEGY_ONLY",
        "trade_blocking": False,
        "order_execution": False,
    }


def apply_free_regime_indicators_v1_patch() -> bool:
    try:
        from bot import adaptive_model_v2 as model

        if getattr(model, "FREE_REGIME_INDICATORS_V1_APPLIED", False):
            return True
        previous_vector = model.feature_vector
        previous_rows = model._training_rows

        def feature_vector_with_free_indicators(*, market, base, option, news, global_market):
            output = dict(
                previous_vector(
                    market=market,
                    base=base,
                    option=option,
                    news=news,
                    global_market=global_market,
                )
            )
            output.update(free_regime_features(market))
            return output

        model.feature_vector = feature_vector_with_free_indicators
        model.FEATURE_NAMES.extend(name for name in FEATURES if name not in model.FEATURE_NAMES)
        model.FEATURE_GROUPS["FREE_REGIME_INDICATORS"] = FEATURES

        def training_rows_with_free_indicators(horizon):
            x_rows, labels = previous_rows(horizon)
            conn = model.get_db()
            try:
                stored_rows = conn.execute(
                    """SELECT s.feature_json FROM ai_advanced_v2_snapshots s
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
                for name, index in indices.items():
                    row_values[index] = _f(persisted.get(name))
                rebuilt.append(row_values)
            return rebuilt, labels

        model._training_rows = training_rows_with_free_indicators
        model.VERSION = f"{model.VERSION}+{VERSION}"
        model.FREE_REGIME_INDICATORS_V1_APPLIED = True
        model.TRAINING_ONLY = True
        model.DECISION_AUTHORITY = "BASELINE_STRATEGY_ONLY"
        return True
    except Exception:
        return False


__all__ = [
    "FEATURES",
    "SQUEEZE_LENGTH",
    "VERSION",
    "apply_free_regime_indicators_v1_patch",
    "completed_chart_candles",
    "free_regime_features",
    "free_regime_status",
]
