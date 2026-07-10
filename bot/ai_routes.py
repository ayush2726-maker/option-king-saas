from datetime import datetime, timezone, timedelta
import hmac
import os
from typing import Any, Dict

from fastapi import APIRouter, Body, Header, HTTPException

from auth.routes import get_current_user
from bot.angel_fetcher import get_user_bot_state
from bot.shared_ai import MODEL_VERSION, predict


router = APIRouter(tags=["Shared Railway AI"])


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _market_open_ist(now_utc: datetime) -> bool:
    ist = now_utc + timedelta(hours=5, minutes=30)
    minutes = ist.hour * 60 + ist.minute
    return ist.weekday() < 5 and (9 * 60 + 15) <= minutes <= (15 * 60 + 30)


def _feed_age_ms(updated_at, now_utc: datetime) -> int:
    if not updated_at:
        return 999999
    try:
        parsed = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((now_utc - parsed.astimezone(timezone.utc)).total_seconds() * 1000))
    except Exception:
        return 999999


def _signal_direction(value):
    text = str(value or "").upper()
    if "CE" in text or text in {"BUY", "BULLISH", "UP", "UPTREND"}:
        return "CE"
    if "PE" in text or text in {"SELL", "BEARISH", "DOWN", "DOWNTREND"}:
        return "PE"
    return "WAIT"


def _user_snapshot(user_id: int) -> Dict[str, Any]:
    state = dict(get_user_bot_state(user_id) or {})
    now_utc = datetime.now(timezone.utc)
    updated_at = state.get("updated_at")
    feed_age_ms = _feed_age_ms(updated_at, now_utc)
    price = _to_float(state.get("price"), 0.0)
    status = str(state.get("status") or "NOT_STARTED")
    strategy = str(state.get("strategy") or "")
    signal = str(state.get("signal") or "WAITING")

    engine_ready = strategy == "TQU_ENHANCED" and price > 0
    feed_connected = bool(engine_ready and feed_age_ms <= 130000 and not status.startswith("ERROR"))

    return {
        "source": "SAAS_RAILWAY_ENGINE",
        "symbol": state.get("underlying") or "NIFTY",
        "price": price,
        "signal": signal,
        "signal_direction": _signal_direction(signal),
        "strategy_score": int(_to_float(state.get("score"), 0)),
        "min_strategy_score": int(
            _to_float(state.get("min_score_required", state.get("min_score", 82)), 82)
        ),
        "server_trade_allowed": bool(state.get("trade_allowed", False)),
        "adx": _to_float(state.get("adx"), 0.0),
        "volume_ratio": _to_float(state.get("volume_ratio"), 0.0),
        "mtf_confirmed": bool(state.get("mtf_confirmed", False)),
        "market_regime": state.get("market_regime") or state.get("regime") or "",
        "warnings": state.get("warnings") or [],
        "strategy": strategy,
        "engine_status": status,
        "engine_updated_at": updated_at,
        "feed_age_ms": feed_age_ms,
        "feed_connected": feed_connected,
        "market_open": _market_open_ist(now_utc),
        "has_open_position": bool(state.get("active_trade") or state.get("has_open_position")),
        "server_time": now_utc.isoformat(),
    }


def _require_personal_ai_key(x_ai_key: str | None) -> None:
    expected = os.getenv("OKAI_AI_API_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="OKAI_AI_API_KEY is not configured on Railway")
    provided = str(x_ai_key or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid AI API key")


@router.get("/ai/health")
def ai_health():
    return {
        "success": True,
        "service": "Option King Shared Railway AI",
        "model_version": MODEL_VERSION,
        "personal_api_key_configured": bool(os.getenv("OKAI_AI_API_KEY", "").strip()),
        "order_execution": False,
    }


@router.post("/ai/predict")
def shared_ai_predict(
    snapshot: Dict[str, Any] = Body(...),
    x_ai_key: str | None = Header(None, alias="X-AI-Key"),
):
    """Prediction endpoint used by the personal bot.

    It receives market features only. Broker credentials and order instructions
    must never be sent to this endpoint.
    """
    _require_personal_ai_key(x_ai_key)
    result = predict(dict(snapshot or {}))
    result["decision_location"] = "RAILWAY_SHARED_AI"
    result["order_execution"] = False
    return result


@router.get("/bot/ai-snapshot")
def get_ai_snapshot(authorization: str = Header(None)):
    user = get_current_user(authorization)
    snapshot = _user_snapshot(user["id"])
    return {
        "success": True,
        "decision_location": "RAILWAY_SHARED_AI",
        **snapshot,
    }


@router.get("/bot/ai-decision")
def get_ai_decision(authorization: str = Header(None)):
    """Authenticated SaaS prediction using the same shared AI core."""
    user = get_current_user(authorization)
    snapshot = _user_snapshot(user["id"])
    result = predict(snapshot)
    return {
        **result,
        "decision_location": "RAILWAY_SHARED_AI",
        "order_execution": False,
        "snapshot": snapshot,
    }
