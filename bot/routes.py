from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from datetime import datetime, timezone
import random

router = APIRouter(prefix="/bot", tags=["Bot"])

# ── In-memory bot state (Railway restart pe reset hoga) ──
_bot_state = {
    "signal": "WAITING",
    "score": 0,
    "adx": 0.0,
    "volume_ratio": 0.0,
    "mtf_confirmed": False,
    "base_score": 0,
    "adx_bonus": 0,
    "volume_bonus": 0,
    "mtf_bonus": 0,
    "regime_score": 0,
    "strategy": "--",
    "updated_at": None,
}

_hero_state = {
    "active": False,
    "current_trade": None,
    "pnl": 0,
    "today_trades": 0,
    "today_wins": 0,
    "today_pnl": 0,
    "capital_used": 0,
}

# ── Signal Endpoint ───────────────────────────────────────
@router.get("/signal")
def get_signal(authorization: str = Header(None)):
    get_current_user(authorization)
    return _bot_state

# ── Hero Status Endpoint ──────────────────────────────────
@router.get("/hero-status")
def get_hero_status(authorization: str = Header(None)):
    get_current_user(authorization)

    now_utc = datetime.now(timezone.utc)
    ist_hour = (now_utc.hour + 5) % 24
    ist_minute = (now_utc.minute + 30) % 60
    if now_utc.minute + 30 >= 60:
        ist_hour = (ist_hour + 1) % 24

    total_min = ist_hour * 60 + ist_minute
    window_start = 14 * 60 + 30  # 14:30
    window_end = 15 * 60          # 15:00

    _hero_state["active"] = window_start <= total_min < window_end
    return _hero_state

# ── Update Signal (internal use - bot calls this) ────────
@router.post("/update-signal")
def update_signal(body: dict, authorization: str = Header(None)):
    get_current_user(authorization)
    _bot_state.update(body)
    _bot_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return {"success": True}

# ── Update Hero State (internal use) ─────────────────────
@router.post("/update-hero")
def update_hero(body: dict, authorization: str = Header(None)):
    get_current_user(authorization)
    _hero_state.update(body)
    return {"success": True}
