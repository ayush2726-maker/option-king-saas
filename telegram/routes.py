from fastapi import APIRouter, Header
from database import (
    get_db,
    get_db_storage_info,
)
from auth.routes import get_current_user
from datetime import datetime
import requests

router = APIRouter(prefix="/telegram", tags=["Telegram"])

def ensure_telegram_settings_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS telegram_settings ("
        "user_id INTEGER PRIMARY KEY, "
        "enabled INTEGER DEFAULT 0, "
        "bot_token TEXT, "
        "chat_id TEXT, "
        "send_bot_alerts INTEGER DEFAULT 1, "
        "send_trade_alerts INTEGER DEFAULT 1, "
        "send_backtest_alerts INTEGER DEFAULT 1, "
        "updated_at TEXT DEFAULT (datetime('now')), "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
        ")"
    )
    conn.commit()


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
    ensure_telegram_settings_table(conn)
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

    send_bot_alerts = 1 if body.get("send_bot_alerts", True) else 0
    send_trade_alerts = 1 if body.get("send_trade_alerts", True) else 0
    send_backtest_alerts = 1 if body.get("send_backtest_alerts", True) else 0

    conn = get_db()
    ensure_telegram_settings_table(conn)

    existing = conn.execute(
        "SELECT bot_token, chat_id "
        "FROM telegram_settings "
        "WHERE user_id=?",
        (user["id"],),
    ).fetchone()

    submitted_token = str(
        body.get("bot_token", "")
    ).strip()
    submitted_chat_id = str(
        body.get("chat_id", "")
    ).strip()

    clear_credentials = bool(
        body.get("clear_credentials", False)
    )

    if clear_credentials:
        bot_token = ""
        chat_id = ""
    else:
        bot_token = (
            submitted_token
            or (
                str(existing["bot_token"] or "")
                if existing
                else ""
            )
        )
        chat_id = (
            submitted_chat_id
            or (
                str(existing["chat_id"] or "")
                if existing
                else ""
            )
        )

    conn.execute(
        "INSERT INTO telegram_settings ("
        "user_id, enabled, bot_token, chat_id, "
        "send_bot_alerts, send_trade_alerts, "
        "send_backtest_alerts, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "enabled=excluded.enabled, "
        "bot_token=excluded.bot_token, "
        "chat_id=excluded.chat_id, "
        "send_bot_alerts=excluded.send_bot_alerts, "
        "send_trade_alerts=excluded.send_trade_alerts, "
        "send_backtest_alerts=excluded.send_backtest_alerts, "
        "updated_at=excluded.updated_at",
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

    storage = get_db_storage_info()

    return {
        "success": True,
        "message": (
            "Telegram settings permanently saved"
            if storage["persistent"]
            else (
                "Telegram settings saved, but "
                "Railway volume is not attached"
            )
        ),
        "permanent_storage": bool(
            storage["persistent"]
        ),
        "volume_attached": bool(
            storage["volume_attached"]
        ),
    }

@router.get("/storage-status")
def telegram_storage_status(
    authorization: str = Header(None),
):
    get_current_user(authorization)
    storage = get_db_storage_info()

    return {
        "success": True,
        "permanent": bool(
            storage["persistent"]
        ),
        "volume_attached": bool(
            storage["volume_attached"]
        ),
        "source": storage["source"],
        "database_exists": bool(
            storage["exists"]
        ),
        "database_size_bytes": int(
            storage["size_bytes"]
        ),
        "message": (
            "Telegram settings persistent volume par safe hain."
            if storage["persistent"]
            else "Railway persistent volume attach nahi hai."
        ),
    }


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
