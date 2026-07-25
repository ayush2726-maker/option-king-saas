"""Explicit saved-broker selection and one-time owner repair.

The broker settings screen can test any saved broker, but a successful test does not
mean that broker is selected.  All runtime/backtest paths correctly read the single
``is_active=1`` row.  This module exposes an authenticated select endpoint and performs
one one-time repair for the owner/admin whose stale Upstox row remained selected while
Angel One credentials were valid.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException

from auth.routes import get_current_user
from auth.utils import decrypt_credential
from bot.brokers.factory import create_broker, get_supported_brokers
from broker.selection import get_selected_broker
from database import get_db


router = APIRouter(prefix="/broker", tags=["Broker"])
MIGRATION_KEY = "ADMIN_SELECT_ANGEL_AFTER_UPSTOX_UI_MISMATCH_20260725_V1"


def _stop_stale_runtime(user_id: int) -> None:
    try:
        from bot.angel_fetcher import reset_user_broker_runtime

        reset_user_broker_runtime(int(user_id))
        return
    except Exception:
        pass

    try:
        from bot.angel_fetcher import stop_user_bot

        stop_user_bot(int(user_id))
    except Exception:
        pass


def _mark_bot_stopped(conn, user_id: int) -> None:
    for table in ("user_bot_state", "bot_status"):
        try:
            conn.execute(
                f"UPDATE {table} SET is_running=0 WHERE user_id=?",
                (int(user_id),),
            )
        except Exception:
            pass


def _login_saved_row(row):
    broker_name = str(row["broker_name"] or "").lower().strip()
    api_key = decrypt_credential(row["api_key"])
    api_secret = decrypt_credential(row["api_secret"])
    totp = (
        decrypt_credential(row["totp_secret"])
        if row["totp_secret"]
        else None
    )
    broker = create_broker(
        broker_name,
        row["client_id"],
        api_key,
        api_secret,
        totp,
    )
    result = broker.login() or {}
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{broker_name.upper()} login failed: "
                + str(result.get("message") or "Unknown broker login error")[:220]
            ),
        )
    return broker_name


@router.post("/select/{broker_name}")
def select_saved_broker(
    broker_name: str,
    authorization: str = Header(None),
):
    """Validate and make one already-saved broker the user's only selected broker."""
    user = get_current_user(authorization)
    requested = str(broker_name or "").lower().strip()
    if requested not in get_supported_brokers():
        raise HTTPException(status_code=400, detail="Unsupported broker")

    conn = get_db()
    try:
        row = conn.execute(
            """SELECT * FROM broker_credentials
               WHERE user_id=? AND broker_name=?
               LIMIT 1""",
            (int(user["id"]), requested),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"{requested.upper()} credentials saved nahi hain.",
            )

        # Never switch the source of orders/data unless this exact saved login works.
        selected_name = _login_saved_row(row)
        now = datetime.utcnow().isoformat()

        conn.execute(
            "UPDATE broker_credentials SET is_active=0 WHERE user_id=?",
            (int(user["id"]),),
        )
        conn.execute(
            """UPDATE broker_credentials
               SET is_active=1, last_connected=?
               WHERE user_id=? AND broker_name=?""",
            (now, int(user["id"]), selected_name),
        )
        _mark_bot_stopped(conn, int(user["id"]))
        conn.commit()
    finally:
        conn.close()

    _stop_stale_runtime(int(user["id"]))
    return {
        "success": True,
        "selected_broker": selected_name,
        "message": (
            f"{selected_name.upper()} is now selected for bot, paper/live data "
            "and every backtest. Start Bot dobara dabayein."
        ),
        "runtime_rebind_required": True,
    }


def repair_admin_angel_selection_once() -> int:
    """One-time repair for the reported owner Upstox/Angel selection mismatch.

    It runs only once, only for admin rows currently selecting Upstox, and only when
    an Angel One credential row already exists.  Future user broker switches are not
    overwritten because the migration marker prevents another repair.
    """
    conn = get_db()
    repaired = 0
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS system_migrations (
                   migration_key TEXT PRIMARY KEY,
                   applied_at TEXT NOT NULL,
                   details TEXT
               )"""
        )
        existing = conn.execute(
            "SELECT migration_key FROM system_migrations WHERE migration_key=?",
            (MIGRATION_KEY,),
        ).fetchone()
        if existing:
            return 0

        rows = conn.execute(
            """SELECT u.id AS user_id
               FROM users u
               JOIN broker_credentials active
                 ON active.user_id=u.id
                AND active.is_active=1
                AND LOWER(active.broker_name)='upstox'
               JOIN broker_credentials angel
                 ON angel.user_id=u.id
                AND LOWER(angel.broker_name)='angelone'
               WHERE COALESCE(u.is_admin, 0)=1"""
        ).fetchall()

        now = datetime.utcnow().isoformat()
        for row in rows:
            user_id = int(row["user_id"])
            conn.execute(
                "UPDATE broker_credentials SET is_active=0 WHERE user_id=?",
                (user_id,),
            )
            updated = conn.execute(
                """UPDATE broker_credentials
                   SET is_active=1, last_connected=?
                   WHERE user_id=? AND LOWER(broker_name)='angelone'""",
                (now, user_id),
            )
            if updated.rowcount:
                _mark_bot_stopped(conn, user_id)
                repaired += 1

        conn.execute(
            """INSERT INTO system_migrations
               (migration_key, applied_at, details)
               VALUES (?, ?, ?)""",
            (
                MIGRATION_KEY,
                now,
                f"admin_angel_rows_selected={repaired}",
            ),
        )
        conn.commit()
        return repaired
    finally:
        conn.close()


def selected_broker_name(user_id: int) -> str | None:
    conn = get_db()
    try:
        row = get_selected_broker(conn, int(user_id))
        return (
            str(row["broker_name"] or "").lower()
            if row is not None
            else None
        )
    finally:
        conn.close()
