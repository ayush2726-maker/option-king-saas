from fastapi import APIRouter, Header, Request, HTTPException
from database import (
    get_db,
    get_db_storage_info,
)
from auth.routes import get_current_user
from datetime import datetime
from html import escape
import os
import secrets
import requests

router = APIRouter(prefix="/telegram", tags=["Telegram"])


def _now_iso():
    return datetime.utcnow().isoformat()


def _global_bot_token():
    return (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("OKAI_TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()


def _global_bot_username():
    raw = (
        os.getenv("TELEGRAM_BOT_USERNAME")
        or os.getenv("OKAI_TELEGRAM_BOT_USERNAME")
        or ""
    ).strip()
    return raw[1:] if raw.startswith("@") else raw


def _resolve_bot_username(bot_token: str):
    configured = _global_bot_username()
    if configured:
        return configured
    if not bot_token:
        return ""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=8,
        )
        data = r.json()
        if r.ok and data.get("ok"):
            return str(data.get("result", {}).get("username") or "").strip()
    except Exception:
        pass
    return ""


def _telegram_table_columns(conn):
    try:
        return {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(telegram_settings)").fetchall()
        }
    except Exception:
        return set()


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
    columns = _telegram_table_columns(conn)
    additions = [
        ("connect_token", "TEXT"),
        ("connected_at", "TEXT"),
        ("last_chat_title", "TEXT"),
    ]
    for name, sql_type in additions:
        if name not in columns:
            try:
                conn.execute(
                    f"ALTER TABLE telegram_settings ADD COLUMN {name} {sql_type}"
                )
            except Exception:
                pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegram_connect_token "
            "ON telegram_settings(connect_token)"
        )
    except Exception:
        pass
    conn.commit()


def send_telegram_message(bot_token: str, chat_id: str, text: str):
    if not bot_token or not chat_id:
        return {"success": False, "message": "Telegram bot token/chat id missing"}

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
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
    except Exception as exc:
        return {"success": False, "message": str(exc)[:180]}


def get_telegram_settings(user_id: int):
    conn = get_db()
    ensure_telegram_settings_table(conn)
    row = conn.execute(
        "SELECT * FROM telegram_settings WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row


def _row_flag(row, key: str, default=True):
    try:
        if key in row.keys():
            return bool(row[key])
    except Exception:
        pass
    return bool(default)


def _row_text(row, key: str):
    try:
        if key in row.keys():
            return str(row[key] or "").strip()
    except Exception:
        pass
    return ""


def notify_user(user_id: int, text: str, alert_type: str = "bot"):
    row = get_telegram_settings(user_id)
    if not row or not row["enabled"]:
        return {"success": False, "message": "Telegram disabled"}

    alert_key = {
        "bot": "send_bot_alerts",
        "trade": "send_trade_alerts",
        "backtest": "send_backtest_alerts",
    }.get(str(alert_type or "bot").lower(), "send_bot_alerts")

    if not _row_flag(row, alert_key, True):
        return {"success": False, "message": "Telegram alert type disabled"}

    bot_token = _global_bot_token() or _row_text(row, "bot_token")
    chat_id = _row_text(row, "chat_id")

    return send_telegram_message(bot_token, chat_id, text)


def notify_trade_alert(user_id: int, text: str):
    return notify_user(user_id, text, alert_type="trade")


@router.get("/settings")
def get_settings(authorization: str = Header(None)):
    user = get_current_user(authorization)
    row = get_telegram_settings(user["id"])
    global_token = bool(_global_bot_token())
    bot_username = _global_bot_username()

    if not row:
        return {
            "success": True,
            "settings": {
                "enabled": False,
                "connected": False,
                "bot_token": "",
                "chat_id": "",
                "send_bot_alerts": True,
                "send_trade_alerts": True,
                "send_backtest_alerts": True,
                "using_global_bot": global_token,
                "bot_username": bot_username,
            }
        }

    return {
        "success": True,
        "settings": {
            "enabled": bool(row["enabled"]),
            "connected": bool(row["enabled"] and _row_text(row, "chat_id")),
            "bot_token": "" if global_token else _row_text(row, "bot_token"),
            "chat_id": _row_text(row, "chat_id"),
            "send_bot_alerts": bool(row["send_bot_alerts"]),
            "send_trade_alerts": bool(row["send_trade_alerts"]),
            "send_backtest_alerts": bool(row["send_backtest_alerts"]),
            "using_global_bot": global_token,
            "bot_username": bot_username,
            "connected_at": _row_text(row, "connected_at"),
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
        "SELECT bot_token, chat_id, connect_token, connected_at, last_chat_title "
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
        connect_token = ""
        connected_at = ""
        last_chat_title = ""
        enabled = 0
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
        connect_token = (
            str(existing["connect_token"] or "")
            if existing
            else ""
        )
        connected_at = (
            str(existing["connected_at"] or "")
            if existing
            else ""
        )
        last_chat_title = (
            str(existing["last_chat_title"] or "")
            if existing
            else ""
        )

    conn.execute(
        "INSERT INTO telegram_settings ("
        "user_id, enabled, bot_token, chat_id, "
        "send_bot_alerts, send_trade_alerts, "
        "send_backtest_alerts, connect_token, connected_at, "
        "last_chat_title, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "enabled=excluded.enabled, "
        "bot_token=excluded.bot_token, "
        "chat_id=excluded.chat_id, "
        "send_bot_alerts=excluded.send_bot_alerts, "
        "send_trade_alerts=excluded.send_trade_alerts, "
        "send_backtest_alerts=excluded.send_backtest_alerts, "
        "connect_token=excluded.connect_token, "
        "connected_at=excluded.connected_at, "
        "last_chat_title=excluded.last_chat_title, "
        "updated_at=excluded.updated_at",
        (
            user["id"],
            enabled,
            bot_token,
            chat_id,
            send_bot_alerts,
            send_trade_alerts,
            send_backtest_alerts,
            connect_token,
            connected_at,
            last_chat_title,
            _now_iso()
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


@router.post("/connect-link")
def create_connect_link(authorization: str = Header(None)):
    user = get_current_user(authorization)
    bot_token = _global_bot_token()
    username = _resolve_bot_username(bot_token)

    if not bot_token or not username:
        return {
            "success": False,
            "message": (
                "Server par TELEGRAM_BOT_TOKEN aur TELEGRAM_BOT_USERNAME set karo. "
                "Uske baad Connect Telegram button kaam karega."
            ),
            "config_required": True,
        }

    token = secrets.token_urlsafe(24)
    conn = get_db()
    ensure_telegram_settings_table(conn)
    now = _now_iso()
    existing = conn.execute(
        "SELECT bot_token, chat_id, send_bot_alerts, send_trade_alerts, "
        "send_backtest_alerts, connected_at, last_chat_title "
        "FROM telegram_settings WHERE user_id=?",
        (user["id"],),
    ).fetchone()
    conn.execute(
        "INSERT INTO telegram_settings ("
        "user_id, enabled, bot_token, chat_id, send_bot_alerts, "
        "send_trade_alerts, send_backtest_alerts, connect_token, "
        "connected_at, last_chat_title, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "connect_token=excluded.connect_token, "
        "send_bot_alerts=excluded.send_bot_alerts, "
        "send_trade_alerts=excluded.send_trade_alerts, "
        "send_backtest_alerts=excluded.send_backtest_alerts, "
        "updated_at=excluded.updated_at",
        (
            user["id"],
            1 if existing and existing["chat_id"] else 0,
            str(existing["bot_token"] or "") if existing else "",
            str(existing["chat_id"] or "") if existing else "",
            int(existing["send_bot_alerts"]) if existing else 1,
            int(existing["send_trade_alerts"]) if existing else 1,
            int(existing["send_backtest_alerts"]) if existing else 1,
            token,
            str(existing["connected_at"] or "") if existing else "",
            str(existing["last_chat_title"] or "") if existing else "",
            now,
        ),
    )
    conn.commit()
    conn.close()

    return {
        "success": True,
        "connect_url": f"https://t.me/{username}?start={token}",
        "bot_username": username,
        "instructions": "Telegram open hoga. Sirf Start dabana hai; chat_id automatic save ho jayegi.",
    }


def _chat_title(chat: dict, from_user: dict):
    parts = [
        str(chat.get("title") or "").strip(),
        str(from_user.get("first_name") or "").strip(),
        str(from_user.get("last_name") or "").strip(),
        str(from_user.get("username") or "").strip(),
    ]
    return " ".join(x for x in parts if x)[:120]


def _complete_telegram_connect(connect_token: str, chat: dict, from_user: dict):
    token = str(connect_token or "").strip()
    chat_id = str(chat.get("id") or "").strip()
    if not token or not chat_id:
        return {"success": True, "ignored": True}

    conn = get_db()
    ensure_telegram_settings_table(conn)
    row = conn.execute(
        "SELECT user_id, bot_token FROM telegram_settings WHERE connect_token=?",
        (token,),
    ).fetchone()

    if not row:
        conn.close()
        send_telegram_message(
            _global_bot_token(),
            chat_id,
            "⚠️ Link expire ya invalid hai. App me dobara Connect Telegram dabayein.",
        )
        return {"success": True, "linked": False, "message": "Invalid token"}

    now = _now_iso()
    title = _chat_title(chat, from_user)
    conn.execute(
        "UPDATE telegram_settings SET enabled=1, chat_id=?, "
        "connect_token=NULL, connected_at=?, last_chat_title=?, updated_at=? "
        "WHERE user_id=?",
        (chat_id, now, title, now, row["user_id"]),
    )
    conn.commit()
    conn.close()

    send_telegram_message(
        _global_bot_token() or str(row["bot_token"] or ""),
        chat_id,
        "✅ <b>Option King AI Telegram Connected</b>\n\n"
        "Ab bot start/stop, trade entry, exit, SL/target aur order-fail alerts yahin aayenge.",
    )

    return {"success": True, "linked": True, "user_id": row["user_id"]}


@router.post("/webhook")
async def telegram_webhook(request: Request):
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if secret:
        received = request.headers.get("x-telegram-bot-api-secret-token", "")
        if received != secret:
            raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")

    try:
        body = await request.json()
    except Exception:
        body = {}

    message = body.get("message") or body.get("edited_message") or {}
    if not isinstance(message, dict):
        return {"success": True, "ignored": True}

    text = str(message.get("text") or "").strip()
    if not text.startswith("/start"):
        return {"success": True, "ignored": True}

    parts = text.split(maxsplit=1)
    connect_token = parts[1].strip() if len(parts) > 1 else ""
    return _complete_telegram_connect(
        connect_token,
        message.get("chat") or {},
        message.get("from") or {},
    )


@router.post("/disconnect")
def disconnect_telegram(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    ensure_telegram_settings_table(conn)
    conn.execute(
        "UPDATE telegram_settings SET enabled=0, chat_id='', "
        "connect_token=NULL, connected_at=NULL, updated_at=? WHERE user_id=?",
        (_now_iso(), user["id"]),
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": "Telegram disconnected"}


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
        f"User: {escape(str(user['email']))}\n"
        "Bot alerts, trade alerts aur backtest summary yahan aayenge."
    )

    res = notify_user(user["id"], text)
    return res


@router.on_event("startup")
def install_telegram_trade_alerts():
    try:
        from telegram.trade_alerts_patch import apply_trade_telegram_alerts_patch
        result = apply_trade_telegram_alerts_patch()
        print(
            "Telegram trade alerts patch installed | "
            f"patched={result.get('patched')}"
        )
    except Exception as exc:
        print(f"Telegram trade alerts patch skipped: {str(exc)[:180]}")
