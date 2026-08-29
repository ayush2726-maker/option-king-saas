from pathlib import Path

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import FileResponse
from database import get_db
from auth.routes import get_current_user
from admin.pnl_report import build_all_user_pnl_report
from admin.user_roles import promote_user_to_admin

router = APIRouter(prefix="/admin", tags=["Admin"])
ADMIN_PANEL_FILE = Path(__file__).with_name("panel.html")


def require_admin(authorization: str):
    user = get_current_user(authorization)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/panel", include_in_schema=False)
def admin_panel():
    """Serve the login shell; every data request remains admin-token protected."""
    return FileResponse(
        ADMIN_PANEL_FILE,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/dashboard")
def admin_dashboard(authorization: str = Header(None)):
    require_admin(authorization)

    conn = get_db()

    total_users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    trial_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE subscription_status='trial'").fetchone()["c"]
    active_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE subscription_status='active'").fetchone()["c"]
    expired_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE subscription_status='expired'").fetchone()["c"]

    total_revenue = conn.execute(
        "SELECT SUM(amount) as r FROM subscriptions WHERE status='active'"
    ).fetchone()["r"] or 0

    bots_running = conn.execute(
        "SELECT COUNT(*) as c FROM bot_status WHERE is_running=1"
    ).fetchone()["c"]

    recent_users = conn.execute(
        """SELECT id, name, email, subscription_status, trial_ends_at, created_at
           FROM users ORDER BY created_at DESC LIMIT 10"""
    ).fetchall()

    conn.close()

    return {
        "success": True,
        "stats": {
            "total_users": total_users,
            "trial_users": trial_users,
            "active_subscribers": active_users,
            "expired_users": expired_users,
            "total_revenue": round(total_revenue, 2),
            "bots_running": bots_running
        },
        "recent_users": [dict(u) for u in recent_users]
    }


@router.get("/users")
def list_all_users(
    page: int = 1,
    limit: int = 20,
    status: str = None,
    authorization: str = Header(None)
):
    require_admin(authorization)

    conn = get_db()
    offset = (page - 1) * limit

    if status:
        users = conn.execute(
            """SELECT id, name, email, phone, subscription_status, trial_ends_at,
                      is_active, is_admin, created_at
               FROM users WHERE subscription_status=?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (status, limit, offset)
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE subscription_status=?", (status,)
        ).fetchone()["c"]
    else:
        users = conn.execute(
            """SELECT id, name, email, phone, subscription_status, trial_ends_at,
                      is_active, is_admin, created_at
               FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]

    conn.close()

    return {
        "success": True,
        "total": total,
        "page": page,
        "users": [dict(u) for u in users]
    }


@router.get("/users/pnl")
def all_users_pnl(authorization: str = Header(None)):
    """Admin-only current and all-time net P&L for every user."""
    require_admin(authorization)
    conn = get_db()
    try:
        return build_all_user_pnl_report(conn)
    finally:
        conn.close()


@router.post("/users/{user_id}/suspend")
def suspend_user(user_id: int, authorization: str = Header(None)):
    require_admin(authorization)

    conn = get_db()
    conn.execute("UPDATE users SET is_active=0 WHERE id=?", (user_id,))
    conn.commit()
    conn.close()

    return {"success": True, "message": f"User {user_id} suspended"}


@router.post("/users/{user_id}/activate")
def activate_user(user_id: int, authorization: str = Header(None)):
    require_admin(authorization)

    conn = get_db()
    conn.execute("UPDATE users SET is_active=1 WHERE id=?", (user_id,))
    conn.commit()
    conn.close()

    return {"success": True, "message": f"User {user_id} activated"}


@router.post("/users/{user_id}/extend-trial")
def extend_trial(user_id: int, body: dict, authorization: str = Header(None)):
    require_admin(authorization)

    days = body.get("days", 7)

    conn = get_db()
    from datetime import datetime, timedelta
    new_trial = (datetime.utcnow() + timedelta(days=days)).isoformat()
    conn.execute(
        "UPDATE users SET trial_ends_at=?, subscription_status='trial' WHERE id=?",
        (new_trial, user_id)
    )
    conn.commit()
    conn.close()

    return {"success": True, "message": f"Trial extended by {days} days for user {user_id}"}


@router.post("/make-admin/{user_id}")
def make_admin(user_id: int, authorization: str = Header(None)):
    require_admin(authorization)

    conn = get_db()
    try:
        target = promote_user_to_admin(conn, user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()

    return {
        "success": True,
        "message": (
            f"{target['email']} was already an admin"
            if target["already_admin"]
            else f"{target['email']} is now an admin"
        ),
        "user": target,
    }

# ── Stats Endpoint (mobile app ke liye) ──────────────────
@router.get("/stats")
def get_stats(authorization: str = Header(None)):
    require_admin(authorization)
    conn = get_db()

    total_users = conn.execute(
        "SELECT COUNT(*) as c FROM users"
    ).fetchone()["c"]

    active_subscriptions = conn.execute(
        "SELECT COUNT(*) as c FROM users WHERE subscription_status='active'"
    ).fetchone()["c"]

    trial_users = conn.execute(
        "SELECT COUNT(*) as c FROM users WHERE subscription_status='trial'"
    ).fetchone()["c"]

    total_revenue = conn.execute(
        "SELECT SUM(amount) as r FROM subscriptions WHERE status='active'"
    ).fetchone()["r"] or 0

    try:
        bot_active = conn.execute(
            "SELECT COUNT(*) as c FROM bot_status WHERE is_running=1"
        ).fetchone()["c"] > 0
    except Exception:
        bot_active = False

    try:
        trades_today = conn.execute(
            "SELECT COUNT(*) as c FROM trades WHERE date(created_at)=date('now')"
        ).fetchone()["c"]
    except Exception:
        trades_today = 0

    conn.close()

    return {
        "total_users": total_users,
        "active_subscriptions": active_subscriptions,
        "trial_users": trial_users,
        "total_revenue": round(total_revenue, 2),
        "bot_active": bot_active,
        "trades_today": trades_today,
    }

# ── Bot Control ───────────────────────────────────────────
@router.post("/bot/start")
def bot_start(authorization: str = Header(None)):
    require_admin(authorization)
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO bot_status (id, is_running, started_at) VALUES (1, 1, datetime('now'))"
        )
        conn.commit()
    except Exception:
        pass
    conn.close()
    return {"success": True, "message": "Bot started"}

@router.post("/bot/stop")
def bot_stop(authorization: str = Header(None)):
    require_admin(authorization)
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO bot_status (id, is_running, started_at) VALUES (1, 0, datetime('now'))"
        )
        conn.commit()
    except Exception:
        pass
    conn.close()
    return {"success": True, "message": "Bot stopped"}

# ── Admin Safe User Delete ─────────────────────────────
@router.post("/users/delete-by-email")
def delete_users_by_email(body: dict, authorization: str = Header(None)):
    admin_user = require_admin(authorization)

    raw_emails = body.get("emails") or []
    emails = []
    for value in raw_emails:
        email = str(value or "").strip().lower()
        if email and email not in emails:
            emails.append(email)

    if not emails:
        raise HTTPException(status_code=400, detail="emails list required")

    conn = get_db()
    deleted = []
    skipped = []

    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        def has_user_id(table):
            if table not in tables:
                return False
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            return any(row[1] == "user_id" for row in cols)

        for email in emails:
            user = conn.execute(
                "SELECT id, email, name, is_admin FROM users WHERE lower(email)=?",
                (email,),
            ).fetchone()

            if not user:
                skipped.append({"email": email, "reason": "not_found"})
                continue

            if int(user["id"]) == int(admin_user["id"]) or bool(user["is_admin"]):
                skipped.append({"email": email, "reason": "admin_user_protected"})
                continue

            user_id = int(user["id"])

            for table in list(tables):
                if table == "users":
                    continue
                if has_user_id(table):
                    try:
                        conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
                    except Exception:
                        pass

            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            deleted.append({
                "id": user_id,
                "email": user["email"],
                "name": user["name"],
            })

        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "deleted": deleted,
        "skipped": skipped,
    }
