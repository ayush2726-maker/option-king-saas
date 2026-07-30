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


def _paper_ledger(conn, user_id: int):
    """Use paper_trades as source of truth for AUTO paper capital/equity."""
    try:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status='CLOSED' THEN pnl ELSE 0 END), 0) AS closed_pnl,
                COALESCE(SUM(CASE WHEN status='OPEN' THEN ((COALESCE(last_ltp, entry_price) - entry_price) * qty) ELSE 0 END), 0) AS open_pnl,
                COUNT(*) AS total_trades
            FROM paper_trades
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()
        closed_pnl = float(row["closed_pnl"] or 0) if row else 0.0
        open_pnl = float(row["open_pnl"] or 0) if row else 0.0
        total_trades = int(row["total_trades"] or 0) if row else 0
    except Exception:
        closed_pnl = 0.0
        open_pnl = 0.0
        total_trades = 0

    total_pnl = round(closed_pnl + open_pnl, 2)
    return {
        "closed_pnl": round(closed_pnl, 2),
        "open_pnl": round(open_pnl, 2),
        "total_pnl": total_pnl,
        "total_trades": total_trades,
    }


def _sync_bot_status_from_ledger(conn, user_id: int, ledger: dict):
    """Keep older dashboard cards that read bot_status from showing stale capital."""
    try:
        now = datetime.utcnow().isoformat()
        cur = conn.execute(
            """
            UPDATE bot_status
            SET total_pnl=?, total_trades=?, updated_at=?
            WHERE user_id=?
            """,
            (
                float(ledger.get("closed_pnl", ledger.get("total_pnl", 0)) or 0),
                int(ledger.get("total_trades", 0) or 0),
                now,
                user_id,
            ),
        )
        if cur.rowcount == 0:
            conn.execute(
                """
                INSERT INTO bot_status
                    (user_id, is_running, last_signal, total_trades, total_pnl, updated_at)
                VALUES (?, 0, 'PAPER_CAPITAL_SYNC', ?, ?, ?)
                """,
                (
                    user_id,
                    int(ledger.get("total_trades", 0) or 0),
                    float(ledger.get("closed_pnl", ledger.get("total_pnl", 0)) or 0),
                    now,
                ),
            )
        conn.commit()
    except Exception:
        pass


@router.get("/account")
def paper_account(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    settings = load_settings(conn, user["id"])
    ledger = _paper_ledger(conn, user["id"])
    _sync_bot_status_from_ledger(conn, user["id"], ledger)
    conn.close()

    capital = float(settings.get("paper_capital", 100000) or 100000)
    equity = round(capital + float(ledger["total_pnl"] or 0), 2)

    return {
        "success": True,
        "account": {
            "trading_mode": settings.get("trading_mode", "paper"),
            "paper_capital": capital,
            "paper_base_capital": capital,
            "closed_pnl": ledger["closed_pnl"],
            "open_pnl": ledger["open_pnl"],
            "total_pnl": ledger["total_pnl"],
            "equity": equity,
            "paper_equity": equity,
            "current_capital": equity,
            "total_trades": ledger["total_trades"],
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
    ledger = _paper_ledger(conn, user["id"])
    _sync_bot_status_from_ledger(conn, user["id"], ledger)
    conn.close()

    equity = round(capital + float(ledger["total_pnl"] or 0), 2)

    notify_user(
        user["id"],
        f"💰 <b>Paper Capital Updated</b>\nCapital: ₹{capital:,.0f}\nCurrent: ₹{equity:,.2f}\nMode: PAPER"
    )

    return {
        "success": True,
        "message": "Paper capital updated",
        "paper_capital": capital,
        "paper_base_capital": capital,
        "total_pnl": ledger["total_pnl"],
        "paper_equity": equity,
        "current_capital": equity,
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
            "UPDATE paper_trades SET pnl=0 WHERE user_id=? AND status='CLOSED'",
            (user["id"],),
        )
    except Exception:
        pass

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
        "paper_capital": capital,
        "paper_base_capital": capital,
        "total_pnl": 0,
        "paper_equity": capital,
        "current_capital": capital,
    }
