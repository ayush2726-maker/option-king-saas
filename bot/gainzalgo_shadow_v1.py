"""GainzAlgo V2 Alpha signal ingestion for the adaptive shadow model.

GainzAlgo is a closed TradingView indicator, so its private calculation is not
reimplemented here.  Authenticated TradingView alerts are stored and exposed as
decision-time features.  The features are training/shadow only: this module has
no entry, blocking, sizing, exit, or order authority.
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from database import get_db


VERSION = "OKAI-GAINZALGO-V2-ALPHA-SHADOW-V1"
FEATURES = (
    "gainzalgo_ce",
    "gainzalgo_pe",
    "gainzalgo_no_trade",
    "gainzalgo_confidence",
    "gainzalgo_available",
    "gainzalgo_freshness",
)
DEFAULT_FRESH_SECONDS = 15 * 60


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_time(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        if number > 1_000_000_000:
            return datetime.fromtimestamp(number, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _direction(value: Any) -> str:
    text = str(value or "").strip().upper().replace("_", " ")
    if text in {"BUY", "LONG", "CE", "CALL", "BULLISH", "UP", "UPTREND"}:
        return "CE"
    if text in {"SELL", "SHORT", "PE", "PUT", "BEARISH", "DOWN", "DOWNTREND"}:
        return "PE"
    return "NO_TRADE"


def _symbol(value: Any) -> str:
    text = str(value or "NIFTY").strip().upper()
    if "BANKNIFTY" in text or "NIFTY BANK" in text:
        return "BANKNIFTY"
    if "SENSEX" in text:
        return "SENSEX"
    if "NIFTY" in text:
        return "NIFTY"
    return text.split(":")[-1].replace(" ", "")[:40] or "NIFTY"


def _confidence(value: Any) -> float:
    # GainzAlgo alerts do not always publish a confidence value.  A neutral 50%
    # presence strength avoids pretending the vendor signal is certain.
    number = _f(value, 50.0)
    if 0.0 <= number <= 1.0:
        return number
    return max(0.0, min(100.0, number)) / 100.0


def _fresh_seconds() -> int:
    return max(
        60,
        min(3600, int(_f(os.getenv("GAINZALGO_SIGNAL_FRESH_SECONDS"), DEFAULT_FRESH_SECONDS))),
    )


def ensure_gainzalgo_schema() -> None:
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_gainzalgo_v2_signals(
              id TEXT PRIMARY KEY,
              received_at TEXT NOT NULL,
              event_at TEXT NOT NULL,
              symbol TEXT NOT NULL,
              timeframe TEXT,
              direction TEXT NOT NULL,
              confidence REAL NOT NULL,
              price REAL,
              provider TEXT NOT NULL DEFAULT 'GAINZALGO_V2_ALPHA',
              payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_ai_gainzalgo_symbol_event
              ON ai_gainzalgo_v2_signals(symbol,event_at DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_gainzalgo_signal(payload: Mapping[str, Any]) -> Dict[str, Any]:
    ensure_gainzalgo_schema()
    raw = dict(payload or {})
    direction = _direction(
        raw.get("signal")
        or raw.get("action")
        or raw.get("side")
        or raw.get("direction")
        or raw.get("order_action")
    )
    symbol = _symbol(raw.get("symbol") or raw.get("ticker") or raw.get("instrument"))
    received = datetime.now(timezone.utc)
    event = _parse_time(
        raw.get("event_at") or raw.get("timestamp") or raw.get("time") or raw.get("timenow")
    ) or received
    # Reject alerts dated far into the future; they cannot be decision-time data.
    if (event - received).total_seconds() > 120:
        raise ValueError("GAINZALGO_EVENT_TIME_IN_FUTURE")
    row_id = str(raw.get("event_id") or raw.get("id") or uuid.uuid4().hex[:24])
    confidence_value = raw.get("confidence")
    if confidence_value is None:
        confidence_value = raw.get("strength")
    confidence = _confidence(confidence_value)
    safe_payload = {key: value for key, value in raw.items() if "secret" not in str(key).lower()}
    conn = get_db()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO ai_gainzalgo_v2_signals(
            id,received_at,event_at,symbol,timeframe,direction,confidence,
            price,provider,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                row_id,
                _iso(received),
                _iso(event),
                symbol,
                str(raw.get("timeframe") or raw.get("interval") or "")[:20],
                direction,
                confidence,
                _f(raw.get("price") or raw.get("close")) or None,
                "GAINZALGO_V2_ALPHA",
                json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))[:8000],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "success": True,
        "id": row_id,
        "symbol": symbol,
        "direction": direction,
        "confidence_percent": round(confidence * 100.0, 2),
        "event_at": _iso(event),
        "mode": "TRAINING_ONLY_SHADOW",
        "trade_blocking": False,
        "order_execution": False,
    }


def latest_gainzalgo_signal(symbol: Any) -> Optional[Dict[str, Any]]:
    ensure_gainzalgo_schema()
    normalized = _symbol(symbol)
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT * FROM ai_gainzalgo_v2_signals
            WHERE symbol=? ORDER BY datetime(event_at) DESC,rowid DESC LIMIT 1""",
            (normalized,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def gainzalgo_features(market: Mapping[str, Any]) -> Dict[str, float]:
    row = latest_gainzalgo_signal(market.get("symbol") or market.get("underlying"))
    if not row:
        return {name: 0.0 for name in FEATURES}
    event = _parse_time(row.get("event_at"))
    age = max(0.0, time.time() - event.timestamp()) if event else float("inf")
    freshness = max(0.0, 1.0 - age / float(_fresh_seconds()))
    if freshness <= 0.0:
        return {name: 0.0 for name in FEATURES}
    direction = _direction(row.get("direction"))
    return {
        "gainzalgo_ce": 1.0 if direction == "CE" else 0.0,
        "gainzalgo_pe": 1.0 if direction == "PE" else 0.0,
        "gainzalgo_no_trade": 1.0 if direction == "NO_TRADE" else 0.0,
        "gainzalgo_confidence": max(0.0, min(1.0, _f(row.get("confidence")))),
        "gainzalgo_available": 1.0,
        "gainzalgo_freshness": freshness,
    }


def gainzalgo_status(symbol: Any = "NIFTY") -> Dict[str, Any]:
    row = latest_gainzalgo_signal(symbol)
    return {
        "success": True,
        "version": VERSION,
        "symbol": _symbol(symbol),
        "configured": bool(os.getenv("GAINZALGO_WEBHOOK_SECRET", "").strip()),
        "latest_signal": row,
        "features": gainzalgo_features({"symbol": symbol}),
        "mode": "TRAINING_ONLY_SHADOW",
        "decision_authority": "BASELINE_STRATEGY_ONLY",
        "trade_blocking": False,
        "order_execution": False,
    }


def apply_gainzalgo_shadow_v1_patch() -> bool:
    try:
        from bot import adaptive_model_v2 as model

        if getattr(model, "GAINZALGO_SHADOW_V1_APPLIED", False):
            return True
        previous_vector = model.feature_vector
        previous_rows = model._training_rows

        def feature_vector_with_gainzalgo(*, market, base, option, news, global_market):
            output = dict(
                previous_vector(
                    market=market,
                    base=base,
                    option=option,
                    news=news,
                    global_market=global_market,
                )
            )
            output.update(gainzalgo_features(market))
            return output

        model.feature_vector = feature_vector_with_gainzalgo
        model.FEATURE_NAMES.extend(name for name in FEATURES if name not in model.FEATURE_NAMES)
        model.FEATURE_GROUPS["GAINZALGO_SHADOW"] = FEATURES

        def training_rows_with_gainzalgo(horizon):
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

        model._training_rows = training_rows_with_gainzalgo
        model.VERSION = f"{model.VERSION}+{VERSION}"
        model.GAINZALGO_SHADOW_V1_APPLIED = True
        model.TRAINING_ONLY = True
        model.DECISION_AUTHORITY = "BASELINE_STRATEGY_ONLY"
        return True
    except Exception:
        return False


__all__ = [
    "FEATURES",
    "VERSION",
    "apply_gainzalgo_shadow_v1_patch",
    "ensure_gainzalgo_schema",
    "gainzalgo_status",
    "record_gainzalgo_signal",
]
