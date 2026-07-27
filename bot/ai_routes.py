from datetime import datetime, timezone, timedelta
import hmac
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Header, HTTPException

from auth.routes import get_current_user
from bot.angel_fetcher import get_user_bot_state
from bot.shared_ai import MODEL_VERSION, predict
from bot.ai_shadow_monitor import (
    get_shadow_summary,
    shadow_monitor_health,
    start_railway_shadow_monitor,
)
from bot.news_intelligence import (
    get_news_summary,
    news_health,
    start_news_intelligence,
)
from bot.advanced_intelligence_v2 import (
    advanced_health,
    get_advanced_summary,
    start_advanced_intelligence,
)
from bot.adaptive_model_v2 import model_status
from bot.broker_intelligence import BROKER_CAPABILITIES
from bot.advanced_ai_data_recovery_patch import (
    apply_advanced_ai_data_recovery_patch,
)

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
    engine_ready = (
        strategy in {"TQU_ENHANCED", "CUSTOM_PROFILE_V1"}
        or state.get("engine_mode") == "AUTO_PORTFOLIO_V1"
    )
    engine_ready = bool(engine_ready and price > 0)
    feed_connected = bool(
        engine_ready and feed_age_ms <= 130000 and not status.startswith("ERROR")
    )
    return {
        "source": "SAAS_RAILWAY_ENGINE",
        "symbol": state.get("underlying") or state.get("chart_instrument") or "NIFTY",
        "price": price,
        "signal": signal,
        "signal_direction": _signal_direction(signal),
        "strategy_score": int(_to_float(state.get("score"), 0)),
        "min_strategy_score": int(
            _to_float(state.get("min_score_required", state.get("min_score", 82)), 82)
        ),
        "server_trade_allowed": bool(state.get("trade_allowed", False)),
        "ema_fast": _to_float(state.get("ema9", state.get("ema_fast")), price),
        "ema_slow": _to_float(state.get("ema21", state.get("ema_slow")), price),
        "vwap": _to_float(state.get("vwap"), price),
        "supertrend_direction": (
            state.get("supertrend_direction") or state.get("supertrend_dir")
            or state.get("supertrend") or ""
        ),
        "structure_direction": (
            state.get("structure_direction") or state.get("market_structure") or ""
        ),
        "mtf_direction": state.get("mtf_direction") or state.get("mtf_trend") or "",
        "adx": _to_float(state.get("adx"), 0.0),
        "rsi": _to_float(state.get("rsi"), 50.0),
        "atr": _to_float(state.get("atr"), 0.0),
        "atr_percent": _to_float(state.get("atr_percent"), 0.0),
        "volume_ratio": _to_float(state.get("volume_ratio"), 0.0),
        "spread_percent": _to_float(state.get("spread_percent"), 0.0),
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


def _require_personal_ai_key(x_ai_key: Optional[str]) -> None:
    expected = os.getenv("OKAI_AI_API_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="OKAI_AI_API_KEY is not configured on Railway")
    provided = str(x_ai_key or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid AI API key")


@router.get("/ai/health")
def ai_health():
    monitor = shadow_monitor_health()
    storage = monitor.get("storage") or {}
    news = news_health()
    news_storage = news.get("storage") or {}
    advanced = advanced_health()
    advanced_storage = advanced.get("storage") or {}
    return {
        "success": True,
        "service": "Option King Shared Railway AI",
        "model_version": MODEL_VERSION,
        "personal_api_key_configured": bool(os.getenv("OKAI_AI_API_KEY", "").strip()),
        "order_execution": False,
        "shadow_monitor": {
            "monitor_version": monitor.get("monitor_version"),
            "started": monitor.get("started"),
            "thread_alive": monitor.get("thread_alive"),
            "last_cycle_at": monitor.get("last_cycle_at"),
            "last_error": monitor.get("last_error"),
            "location": "RAILWAY",
            "storage_persistent": bool(storage.get("persistent")),
            "trade_blocking": False,
            "order_execution": False,
        },
        "news_intelligence": {
            "news_version": news.get("news_version"),
            "started": news.get("started"),
            "thread_alive": news.get("thread_alive"),
            "last_cycle_at": news.get("last_cycle_at"),
            "last_fetch_at": news.get("last_fetch_at"),
            "last_error": news.get("last_error"),
            "recent_event_count": news.get("recent_event_count"),
            "source_counts": news.get("source_counts"),
            "location": "RAILWAY",
            "storage_persistent": bool(news_storage.get("persistent")),
            "trade_blocking": False,
            "order_execution": False,
        },
        "advanced_intelligence": {
            "version": advanced.get("version"),
            "started": advanced.get("started"),
            "thread_alive": advanced.get("thread_alive"),
            "last_cycle_at": advanced.get("last_cycle_at"),
            "last_error": advanced.get("last_error"),
            "decision_count": advanced.get("decision_count"),
            "pending_count": advanced.get("pending_count"),
            "adaptive_models": advanced.get("adaptive_models"),
            "live_probe_count": advanced.get("live_probe_count"),
            "live_probes": advanced.get("live_probes"),
            "location": "RAILWAY",
            "storage_persistent": bool(advanced_storage.get("persistent")),
            "supported_brokers": ["angelone", "upstox", "zerodha"],
            "trade_blocking": False,
            "order_execution": False,
        },
    }


@router.post("/ai/predict")
def shared_ai_predict(
    snapshot: Dict[str, Any] = Body(...),
    x_ai_key: Optional[str] = Header(None, alias="X-AI-Key"),
):
    _require_personal_ai_key(x_ai_key)
    result = predict(dict(snapshot or {}))
    result["decision_location"] = "RAILWAY_SHARED_AI"
    result["order_execution"] = False
    return result


@router.get("/bot/ai-snapshot")
def get_ai_snapshot(authorization: str = Header(None)):
    user = get_current_user(authorization)
    snapshot = _user_snapshot(user["id"])
    return {"success": True, "decision_location": "RAILWAY_SHARED_AI", **snapshot}


@router.get("/bot/ai-decision")
def get_ai_decision(authorization: str = Header(None)):
    user = get_current_user(authorization)
    snapshot = _user_snapshot(user["id"])
    result = predict(snapshot)
    return {
        **result,
        "decision_location": "RAILWAY_SHARED_AI",
        "order_execution": False,
        "trade_blocking": False,
        "snapshot": snapshot,
    }


@router.get("/bot/ai-shadow-monitor")
def get_ai_shadow_monitor(authorization: str = Header(None), recent_limit: int = 20):
    user = get_current_user(authorization)
    return get_shadow_summary(user["id"], recent_limit=recent_limit)


@router.get("/bot/ai-news-monitor")
def get_ai_news_monitor(authorization: str = Header(None), recent_limit: int = 20):
    user = get_current_user(authorization)
    return get_news_summary(user["id"], recent_limit=recent_limit)


@router.get("/bot/ai-advanced-monitor")
def get_ai_advanced_monitor(authorization: str = Header(None), recent_limit: int = 20):
    user = get_current_user(authorization)
    return get_advanced_summary(user["id"], recent_limit=recent_limit)


@router.get("/bot/ai-model-status")
def get_ai_model_status(authorization: str = Header(None)):
    get_current_user(authorization)
    return model_status()


@router.get("/bot/ai-broker-capabilities")
def get_ai_broker_capabilities(authorization: str = Header(None)):
    get_current_user(authorization)
    return {
        "success": True,
        "brokers": BROKER_CAPABILITIES,
        "core_ai_runs_for_every_supported_broker": True,
        "missing_native_fields_are_derived_or_marked_unavailable": True,
        "trade_blocking": False,
        "order_execution": False,
    }


# Install the shadow-only recovery before starting any monitor threads.  Rebind the
# imported names so the already-defined route functions resolve the patched live
# summary and health functions at request time.
apply_advanced_ai_data_recovery_patch()
from bot import advanced_intelligence_v2 as _advanced_runtime
advanced_health = _advanced_runtime.advanced_health
get_advanced_summary = _advanced_runtime.get_advanced_summary
start_advanced_intelligence = _advanced_runtime.start_advanced_intelligence

start_railway_shadow_monitor(_user_snapshot)
start_news_intelligence(_user_snapshot)
start_advanced_intelligence(_user_snapshot)
