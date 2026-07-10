from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Header

from auth.routes import get_current_user
from bot.angel_fetcher import get_user_bot_state


router = APIRouter(prefix="/bot", tags=["Bot AI Data"])


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


@router.get("/ai-snapshot")
def get_ai_snapshot(authorization: str = Header(None)):
    """
    Raw, read-only market snapshot for on-device AI inference.

    This endpoint does not make an AI decision and does not place, modify,
    or close any order. The mobile app remains responsible for inference.
    """
    user = get_current_user(authorization)
    state = dict(get_user_bot_state(user["id"]) or {})

    now_utc = datetime.now(timezone.utc)
    updated_at = state.get("updated_at")
    feed_age_ms = _feed_age_ms(updated_at, now_utc)
    price = _to_float(state.get("price"), 0.0)
    status = str(state.get("status") or "NOT_STARTED")
    strategy = str(state.get("strategy") or "")
    signal = str(state.get("signal") or "WAITING")

    engine_ready = strategy == "TQU_ENHANCED" and price > 0
    feed_connected = bool(engine_ready and feed_age_ms <= 120000 and not status.startswith("ERROR"))

    return {
        "success": True,
        "source": "SERVER_RAW_SNAPSHOT",
        "decision_location": "APP_PHONE",
        "symbol": state.get("underlying") or "NIFTY",
        "price": price,
        "signal": signal,
        "signal_direction": _signal_direction(signal),
        "score": int(_to_float(state.get("score"), 0)),
        "min_score": int(_to_float(state.get("min_score_required", state.get("min_score", 82)), 82)),
        "trade_allowed": bool(state.get("trade_allowed", False)),
        "adx": _to_float(state.get("adx"), 0.0),
        "volume_ratio": _to_float(state.get("volume_ratio"), 0.0),
        "mtf_confirmed": bool(state.get("mtf_confirmed", False)),
        "warnings": state.get("warnings") or [],
        "strategy": strategy,
        "engine_status": status,
        "engine_updated_at": updated_at,
        "feed_age_ms": feed_age_ms,
        "feed_connected": feed_connected,
        "market_open": _market_open_ist(now_utc),
        "server_time": now_utc.isoformat(),
    }
