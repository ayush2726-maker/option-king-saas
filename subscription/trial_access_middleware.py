import json

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from auth.utils import decode_token
from database import get_db
from subscription.entitlements import entitlement_snapshot


class TrialAccessMiddleware(BaseHTTPMiddleware):
    """Canonical split-trial access guard and legacy-status compatibility.

    LIVE access expires after 7 days. PAPER remains available for 30 days.
    Older routes still inspect users.subscription_status, so while Paper trial
    is valid we keep the overall account status as ``trial`` even after the
    Live portion has expired. Entitlements remain the authority for Live/Paper.
    """

    async def dispatch(self, request, call_next):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return await call_next(request)

        try:
            payload = decode_token(auth.split(" ", 1)[1])
            user_id = int(payload["user_id"])
        except Exception:
            return await call_next(request)

        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                return await call_next(request)
            user = dict(row)
            access = entitlement_snapshot(user)

            # Compatibility for legacy broker/account routes: the overall free
            # trial is still active while Paper access remains. Do not touch
            # paid/admin users and do not resurrect a cleared/expired trial.
            status = str(user.get("subscription_status") or "").lower()
            if (
                not bool(user.get("is_admin"))
                and status != "active"
                and bool(access.get("paper_allowed"))
                and status != "trial"
            ):
                conn.execute(
                    "UPDATE users SET subscription_status='trial' WHERE id=?",
                    (user_id,),
                )
                conn.commit()
                user["subscription_status"] = "trial"

            if request.method.upper() != "POST" or request.url.path != "/bot/start":
                return await call_next(request)

            mode = "paper"
            try:
                settings_row = conn.execute(
                    "SELECT settings_json FROM strategy_settings WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                if settings_row and settings_row["settings_json"]:
                    settings = json.loads(settings_row["settings_json"])
                    mode = str(settings.get("trading_mode") or "paper").lower()
            except Exception:
                mode = "paper"

            if mode == "live" and not access["live_allowed"]:
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "code": "LIVE_TRIAL_EXPIRED",
                        "message": "Live trading trial has ended. Paper trading is still available during the 30-day paper trial.",
                        "entitlements": access,
                    },
                )

            if mode != "live" and not access["paper_allowed"]:
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "code": "PAPER_TRIAL_EXPIRED",
                        "message": "Your 30-day paper trial has ended. Subscribe to continue.",
                        "entitlements": access,
                    },
                )
        finally:
            conn.close()

        return await call_next(request)
