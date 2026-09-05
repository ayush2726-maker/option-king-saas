import json

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from auth.utils import decode_token
from database import get_db
from subscription.entitlements import entitlement_snapshot


class TrialAccessMiddleware(BaseHTTPMiddleware):
    """Server-side access guard for starting PAPER/LIVE bot sessions.

    LIVE access expires after the 7-day trial. PAPER remains available for the
    30-day trial. Paid/admin users retain both. This guard sits at the start
    boundary so an old mobile build cannot bypass the entitlement UI.
    """

    async def dispatch(self, request, call_next):
        if request.method.upper() != "POST" or request.url.path != "/bot/start":
            return await call_next(request)

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
