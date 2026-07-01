from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from datetime import datetime
import requests

router = APIRouter(prefix="/telegram", tags=["Telegram"])

def send_telegram_message(bot_token: str, chat_id: str, text: str):
    if not bot_token or not chat_id:
        return {"success": False, "message": "Telegram bot token/chat id missing"}

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    r = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=15)

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    return {
        "success": r.ok and data.get("ok", False),
        "status_code": r.status_code,
        "response": data
    }

def get_telegram_settings(user_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM telegram_settings WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row

def notify_user(user_id: int, text: str):
    row = get_telegram_settings(user_id)
    if not row or not row["enabled"]:
        return {"success": False, "message": "Telegram disabled"}

    return send_telegram_message(
        row["bot_token"],
        row["chat_id"],
        text
    )

@router.get("/settings")
def get_settings(authorization: str = Header(None)):
    user = get_current_user(authorization)
    row = get_telegram_settings(user["id"])

    if not row:
        return {
            "success": True,
            "settings": {
                "enabled": False,
                "bot_token": "",
                "chat_id": "",
                "send_bot_alerts": True,
                "send_trade_alerts": True,
                "send_backtest_alerts": True
            }
        }

    return {
        "success": True,
        "settings": {
            "enabled": bool(row["enabled"]),
            "bot_token": row["bot_token"],
            "chat_id": row["chat_id"],
            "send_bot_alerts": bool(row["send_bot_alerts"]),
            "send_trade_alerts": bool(row["send_trade_alerts"]),
            "send_backtest_alerts": bool(row["send_backtest_alerts"])
        }
    }

@router.post("/settings")
def save_settings(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    body = body or {}

    enabled = 1 if body.get("enabled", True) else 0
    bot_token = str(body.get("bot_token", "")).strip()
    chat_id = str(body.get("chat_id", "")).strip()

    send_bot_alerts = 1 if body.get("send_bot_alerts", True) else 0
    send_trade_alerts = 1 if body.get("send_trade_alerts", True) else 0
    send_backtest_alerts = 1 if body.get("send_backtest_alerts", True) else 0

    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO telegram_settings
           (user_id, enabled, bot_token, chat_id, send_bot_alerts, send_trade_alerts, send_backtest_alerts, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user["id"],
            enabled,
            bot_token,
            chat_id,
            send_bot_alerts,
            send_trade_alerts,
            send_backtest_alerts,
            datetime.utcnow().isoformat()
        )
    )
    conn.commit()
    conn.close()

    return {"success": True, "message": "Telegram settings saved"}

@router.post("/test")
def test_telegram(authorization: str = Header(None)):
    user = get_current_user(authorization)

    text = (
        "✅ <b>Option King AI Telegram Connected</b>\n\n"
        f"User: {user['email']}\n"
        "Bot alerts, trade alerts aur backtest summary yahan aayenge."
    )

    res = notify_user(user["id"], text)
    return res
