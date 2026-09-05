import os
import threading
import time


def _apply_monthly_plan_price_override():
    """Keep all subscription gateways on the same ₹5,000 / 30-day plan."""
    try:
        from subscription import routes as subscription_routes

        subscription_routes.PLAN_ID = "monthly_5000"
        subscription_routes.PLAN.clear()
        subscription_routes.PLAN.update(
            {
                "id": subscription_routes.PLAN_ID,
                "name": "OKAI Monthly Plan",
                "price": 500000,
                "amount_rupees": 5000,
                "display_price": "₹5,000",
                "duration_days": 30,
                "renewal": "manual",
                "features": [
                    "Full Option King AI access",
                    "Paper and Live trading tools",
                    "Strategy builder and backtests",
                    "Trade alerts and reports",
                    "30 days validity",
                ],
            }
        )
    except Exception as exc:
        print(f"Monthly plan price override skipped: {str(exc)[:160]}")


def _demote_configured_admins_worker():
    """Idempotently demote admin accounts listed in ADMIN_DEMOTE_EMAILS."""
    raw = str(os.getenv("ADMIN_DEMOTE_EMAILS", "")).strip()
    emails = [value.strip().lower() for value in raw.split(",") if value.strip()]
    if not emails:
        return

    # Database tables are created during FastAPI startup. Retry briefly until ready.
    for _ in range(12):
        conn = None
        try:
            from database import get_db

            conn = get_db()
            conn.execute("SELECT id FROM users LIMIT 1").fetchone()
            changed = 0
            for email in emails:
                cursor = conn.execute(
                    "UPDATE users SET is_admin=0 WHERE lower(email)=? AND is_admin=1",
                    (email,),
                )
                changed += max(0, int(cursor.rowcount or 0))
            conn.commit()
            if changed:
                print(f"Admin demotion override applied | accounts={changed}")
            return
        except Exception:
            time.sleep(2)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass


_apply_monthly_plan_price_override()
threading.Thread(target=_demote_configured_admins_worker, daemon=True).start()


def promote_user_to_admin(conn, user_id: int) -> dict:
    """Promote exactly one user without changing any existing administrator."""
    target = conn.execute(
        "SELECT id, name, email, is_admin FROM users WHERE id=? LIMIT 1",
        (int(user_id),),
    ).fetchone()
    if not target:
        raise LookupError("User not found")

    already_admin = bool(target["is_admin"])
    if not already_admin:
        conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (int(user_id),))
        conn.commit()

    return {
        "id": int(target["id"]),
        "name": str(target["name"] or ""),
        "email": str(target["email"] or ""),
        "is_admin": True,
        "already_admin": already_admin,
    }
