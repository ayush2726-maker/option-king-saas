from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from bot.angel_fetcher import start_user_bot, stop_user_bot, get_user_bot_state
from bot.strategy import is_hero_window_active
from datetime import datetime, timezone

router = APIRouter(prefix="/bot", tags=["Bot"])

@router.get("/signal")
def get_signal(authorization: str = Header(None)):
    user = get_current_user(authorization)
    state = get_user_bot_state(user["id"])
    return state

@router.get("/hero-status")
def get_hero_status(authorization: str = Header(None)):
    get_current_user(authorization)
    return is_hero_window_active()

@router.post("/start")
def bot_start(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    broker = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
        (user["id"],)
    ).fetchone()
    conn.close()
    if not broker:
        return {"success": False, "message": "Pehle broker credentials save karo"}
    creds = {
        "api_key":     broker["api_key"],
        "client_id":   broker["client_id"],
        "password":    broker["api_secret"],
        "totp_secret": broker["totp_secret"],
    }
    return start_user_bot(user["id"], creds)

@router.post("/stop")
def bot_stop(authorization: str = Header(None)):
    user = get_current_user(authorization)
    return stop_user_bot(user["id"])

@router.post("/update-signal")
def update_signal(body: dict, authorization: str = Header(None)):
    get_current_user(authorization)
    return {"success": True}
