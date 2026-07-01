from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from strategy.routes import DEFAULT_SETTINGS
from telegram.routes import notify_user
from datetime import datetime
import json

router = APIRouter(prefix="/paper", tags=["Paper"])

def clamp_cap(v):
    try:
        x = float(v)
    except Exception:
        x = 100000
    if x < 1000:
        x = 1000
    if x > 10000000:
        x = 10000000
    return x

def load_settings(conn, user_id: int):
    settings = dict(DEFAULT_SETTINGS)
    row = conn.execute(
        "SELECT settings_json FROM strategy_settings WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if row:
        try:
            settings.update(json.loads(row["settings_json"]))
        except Exception:
            pass

    if "paper_capital" not in settings:
        settings["paper_capital"] = 100000
    if "trading_mode" not in settings:
        settings["trading_mode"] = "paper"

    return settings

def save_settings(conn, user_id: int, settings: dict):
    conn.execute(
        "INSERT OR REPLACE INTO strategy_settings (user_id, settings_json, updated_at) VALUES (?, ?, ?)",
        (user_id, json.dumps(settings), datetime.utcnow().isoformat())
    )
    conn.commit()

@router.get("/account")
def paper_account(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    settings = load_settings(conn, user["id"])

    total_pnl = 0
    total_trades = 0
    try:
        row = conn.execute(
            "SELECT total_pnl, total_trades FROM bot_status WHERE user_id=?",
            (user["id"],)
        ).fetchone()
        if row:
            total_pnl = float(row["total_pnl"] or 0)
            total_trades = int(row["total_trades"] or 0)
    except Exception:
        pass

    conn.close()

    capital = float(settings.get("paper_capital", 100000) or 100000)
    equity = capital + total_pnl

    return {
        "success": True,
        "account": {
            "trading_mode": settings.get("trading_mode", "paper"),
            "paper_capital": capital,
            "total_pnl": total_pnl,
            "equity": equity,
            "total_trades": total_trades
        }
    }

@router.post("/capital")
def update_paper_capital(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    body = body or {}

    capital = clamp_cap(body.get("capital", 100000))

    conn = get_db()
    settings = load_settings(conn, user["id"])
    settings["paper_capital"] = capital
    if body.get("make_paper_mode", True):
        settings["trading_mode"] = "paper"

    save_settings(conn, user["id"], settings)
    conn.close()

    notify_user(
        user["id"],
        f"💰 <b>Paper Capital Updated</b>\nCapital: ₹{capital:,.0f}\nMode: PAPER"
    )

    return {
        "success": True,
        "message": "Paper capital updated",
        "paper_capital": capital,
        "settings": settings
    }

@router.post("/reset")
def reset_paper_account(body: dict = None, authorization: str = Header(None)):
    user = get_current_user(authorization)
    body = body or {}

    capital = clamp_cap(body.get("capital", 100000))

    conn = get_db()
    settings = load_settings(conn, user["id"])
    settings["paper_capital"] = capital
    settings["trading_mode"] = "paper"
    save_settings(conn, user["id"], settings)

    try:
        conn.execute(
            """UPDATE bot_status
               SET total_pnl=0, total_trades=0, last_signal='PAPER_RESET', updated_at=?
               WHERE user_id=?""",
            (datetime.utcnow().isoformat(), user["id"])
        )
        conn.commit()
    except Exception:
        pass

    conn.close()

    notify_user(
        user["id"],
        f"♻️ <b>Paper Account Reset</b>\nCapital: ₹{capital:,.0f}\nP&L reset to 0."
    )

    return {
        "success": True,
        "message": "Paper account reset",
        "paper_capital": capital
    }
