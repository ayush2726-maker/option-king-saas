from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from strategy.routes import DEFAULT_SETTINGS
from telegram.routes import notify_user
from datetime import datetime, timedelta
import json

router = APIRouter(tags=["User Panel"])

def is_admin_user(user):
    return bool(user.get("is_admin") or user.get("role") == "admin")

def row_to_dict(row):
    try:
        return dict(row)
    except Exception:
        return {}

def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT,
            side TEXT,
            entry_price REAL,
            exit_price REAL,
            qty INTEGER DEFAULT 0,
            pnl REAL DEFAULT 0,
            status TEXT DEFAULT 'CLOSED',
            reason TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_name TEXT,
            amount REAL DEFAULT 0,
            note TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

def table_columns(conn, table):
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r["name"] for r in rows]
    except Exception:
        return []

def load_settings(conn, user_id):
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
    settings.setdefault("trading_mode", "paper")
    settings.setdefault("paper_capital", 100000)
    return settings

def save_settings(conn, user_id, settings):
    conn.execute(
        "INSERT OR REPLACE INTO strategy_settings (user_id, settings_json, updated_at) VALUES (?, ?, ?)",
        (user_id, json.dumps(settings), datetime.utcnow().isoformat())
    )
    conn.commit()

@router.get("/user/profile")
def user_profile(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    ensure_tables(conn)

    settings = load_settings(conn, user["id"])
    conn.close()

    return {
        "success": True,
        "profile": {
            "id": user.get("id"),
            "name": user.get("name"),
            "email": user.get("email"),
            "phone": user.get("phone"),
            "subscription_status": user.get("subscription_status"),
            "trial_ends_at": user.get("trial_ends_at"),
            "is_admin": bool(user.get("is_admin")),
            "trading_mode": settings.get("trading_mode", "paper"),
            "paper_capital": settings.get("paper_capital", 100000)
        }
    }

@router.get("/history/trades")
def trade_history(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    ensure_tables(conn)

    trades = []
    try:
        rows = conn.execute(
            "SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT 100",
            (user["id"],)
        ).fetchall()
        trades = [row_to_dict(r) for r in rows]
    except Exception:
        trades = []

    conn.close()
    return {"success": True, "trades": trades}

@router.get("/history/paper")
def paper_history(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    ensure_tables(conn)

    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE user_id=? ORDER BY id DESC LIMIT 100",
        (user["id"],)
    ).fetchall()

    conn.close()
    return {"success": True, "paper_trades": [row_to_dict(r) for r in rows]}

@router.get("/reports/daily")
def daily_report(authorization: str = Header(None)):
    user = get_current_user(authorization)
    today = datetime.utcnow().date().isoformat()

    conn = get_db()
    ensure_tables(conn)

    settings = load_settings(conn, user["id"])

    live_trades = []
    paper_trades = []
    backtests = []

    try:
        rows = conn.execute(
            "SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT 100",
            (user["id"],)
        ).fetchall()
        live_trades = [row_to_dict(r) for r in rows]
    except Exception:
        pass

    try:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE user_id=? ORDER BY id DESC LIMIT 100",
            (user["id"],)
        ).fetchall()
        paper_trades = [row_to_dict(r) for r in rows]
    except Exception:
        pass

    try:
        rows = conn.execute(
            "SELECT * FROM backtest_runs WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (user["id"],)
        ).fetchall()
        backtests = [row_to_dict(r) for r in rows]
    except Exception:
        pass

    def pnl_sum(items):
        total = 0.0
        for x in items:
            for k in ["pnl", "profit", "net_pnl", "total_pnl"]:
                if k in x and x[k] is not None:
                    try:
                        total += float(x[k])
                        break
                    except Exception:
                        pass
        return round(total, 2)

    report = {
        "date": today,
        "trading_mode": settings.get("trading_mode", "paper"),
        "paper_capital": float(settings.get("paper_capital", 100000) or 100000),
        "live_trade_count": len(live_trades),
        "paper_trade_count": len(paper_trades),
        "backtest_count": len(backtests),
        "live_pnl": pnl_sum(live_trades),
        "paper_pnl": pnl_sum(paper_trades),
    }
    report["total_pnl"] = round(report["live_pnl"] + report["paper_pnl"], 2)
    report["paper_equity"] = round(report["paper_capital"] + report["paper_pnl"], 2)

    conn.close()
    return {
        "success": True,
        "report": report,
        "live_trades": live_trades[:20],
        "paper_trades": paper_trades[:20],
        "backtests": backtests[:10]
    }

@router.post("/billing/purchase-request")
def purchase_request(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    body = body or {}

    plan_name = str(body.get("plan_name", "monthly")).strip()
    amount = float(body.get("amount", 0) or 0)
    note = str(body.get("note", "")).strip()

    conn = get_db()
    ensure_tables(conn)
    conn.execute(
        """INSERT INTO plan_requests
           (user_id, plan_name, amount, note, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
        (user["id"], plan_name, amount, note, datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

    notify_user(
        user["id"],
        f"💳 <b>Plan Purchase Request</b>\nPlan: {plan_name}\nAmount: ₹{amount}\nStatus: Pending"
    )

    return {"success": True, "message": "Plan purchase request submitted. Admin approval pending."}

@router.get("/admin/users-lite")
def admin_users(authorization: str = Header(None)):
    user = get_current_user(authorization)
    if not is_admin_user(user):
        return {"success": False, "message": "Admin access required", "users": []}

    conn = get_db()
    ensure_tables(conn)
    rows = conn.execute("SELECT * FROM users ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()

    users = []
    for r in rows:
        d = row_to_dict(r)
        for secret in ["password", "hashed_password", "password_hash"]:
            d.pop(secret, None)
        users.append(d)

    return {"success": True, "users": users}

@router.get("/admin/plan-requests")
def admin_plan_requests(authorization: str = Header(None)):
    user = get_current_user(authorization)
    if not is_admin_user(user):
        return {"success": False, "message": "Admin access required", "requests": []}

    conn = get_db()
    ensure_tables(conn)
    rows = conn.execute(
        "SELECT * FROM plan_requests ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()

    return {"success": True, "requests": [row_to_dict(r) for r in rows]}

@router.post("/admin/activate-user")
def admin_activate_user(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    if not is_admin_user(user):
        return {"success": False, "message": "Admin access required"}

    body = body or {}
    target_user_id = int(body.get("user_id", 0) or 0)
    days = int(body.get("days", 30) or 30)
    if target_user_id <= 0:
        return {"success": False, "message": "Invalid user id"}

    active_until = (datetime.utcnow() + timedelta(days=days)).isoformat()

    conn = get_db()
    cols = table_columns(conn, "users")
    updates = []
    params = []

    if "subscription_status" in cols:
        updates.append("subscription_status=?")
        params.append("active")
    if "trial_ends_at" in cols:
        updates.append("trial_ends_at=?")
        params.append(active_until)

    if updates:
        params.append(target_user_id)
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", params)
        conn.commit()

    conn.close()
    return {"success": True, "message": f"User activated for {days} days", "active_until": active_until}

@router.post("/settings/reset-all")
def reset_all_settings(body: dict = None, authorization: str = Header(None)):
    user = get_current_user(authorization)
    body = body or {}
    capital = float(body.get("paper_capital", 100000) or 100000)

    settings = dict(DEFAULT_SETTINGS)
    settings["trading_mode"] = "paper"
    settings["paper_capital"] = capital

    conn = get_db()
    ensure_tables(conn)
    save_settings(conn, user["id"], settings)

    try:
        conn.execute("DELETE FROM paper_trades WHERE user_id=?", (user["id"],))
        conn.commit()
    except Exception:
        pass

    try:
        conn.execute(
            """UPDATE bot_status
               SET is_running=0, total_pnl=0, total_trades=0, last_signal='RESET', updated_at=?
               WHERE user_id=?""",
            (datetime.utcnow().isoformat(), user["id"])
        )
        conn.commit()
    except Exception:
        pass

    conn.close()

    notify_user(
        user["id"],
        f"♻️ <b>All Settings Reset</b>\nMode: PAPER\nPaper Capital: ₹{capital:,.0f}\nBroker credentials not removed."
    )

    return {"success": True, "message": "All settings reset. Broker credentials not removed.", "settings": settings}
