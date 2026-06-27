from fastapi import APIRouter, HTTPException, Header
from database import get_db
from auth.routes import get_current_user

router = APIRouter(prefix="/admin", tags=["Admin"])


def require_admin(authorization: str):
    user = get_current_user(authorization)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


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
                      is_active, created_at
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
                      is_active, created_at
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
    conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (user_id,))
    conn.commit()
    conn.close()

    return {"success": True, "message": f"User {user_id} is now admin"}
