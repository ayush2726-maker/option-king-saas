from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import init_db
from auth.routes import router as auth_router
from auth.recovery_routes import router as recovery_router, ensure_recovery_schema
from auth.registration_email_middleware import SafeRegistrationEmailVerificationMiddleware
from local_gateway.routes import router as local_gateway_router
from local_gateway.service import ensure_local_gateway_schema
from broker.routes import router as broker_router
from subscription.routes import router as subscription_router
from admin.routes import router as admin_router
from bot.routes import router as bot_router
from telegram.routes import router as telegram_router
from user_panel.routes import router as user_panel_router
from paper.routes import router as paper_router
from strategy.routes import router as strategy_router
from strategy.profile_routes import router as strategy_profile_router
from bot.market_routes import router as market_router
from bot.ai_routes import router as ai_router
from backtest.routes import router as backtest_router
from bot.score_history_patch import apply_score_history_patch
from bot.upstox_live_candle_patch import apply_upstox_live_candle_patch
from bot.live_scan_history_fallback_patch import apply_live_scan_history_fallback_patch
from bot.default_strategy_patch import (
    apply_default_strategy_patch,
    migrate_default_strategy_profiles,
)
from bot.fresh_entry_guard_patch import apply_fresh_entry_guard_patch
from bot.expiry_entry_diagnostics_patch import apply_expiry_entry_diagnostics_patch
from bot.feed_safety_consistency_patch import apply_feed_safety_consistency_patch
from bot.mandatory_trend_structure_patch import apply_mandatory_trend_structure_patch
from bot.expiry_hardlock_one_second_monitor_patch import (
    apply_expiry_hardlock_one_second_monitor_patch,
)
from bot.signal_history_response_middleware import StrictSignalHistoryMiddleware
import os

apply_score_history_patch()
apply_upstox_live_candle_patch()
apply_live_scan_history_fallback_patch()
apply_default_strategy_patch()
apply_fresh_entry_guard_patch()
apply_expiry_entry_diagnostics_patch()
apply_feed_safety_consistency_patch()
apply_mandatory_trend_structure_patch()
apply_expiry_hardlock_one_second_monitor_patch()

RELEASE_VERSION = "expiry-hardlock-one-second-monitor-v1"

app = FastAPI(
    title="Option King AI — SaaS API",
    description="Multi-user F&O trading bot platform",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(StrictSignalHistoryMiddleware)
app.add_middleware(SafeRegistrationEmailVerificationMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()
    ensure_recovery_schema()
    ensure_local_gateway_schema()

    from database import init_bot_status_table
    init_bot_status_table()
    migrate_default_strategy_profiles()

    admin_email = os.getenv("ADMIN_EMAIL")
    admin_password = os.getenv("ADMIN_PASSWORD")
    admin_name = os.getenv("ADMIN_NAME", "Ayush")

    if admin_email and admin_password:
        from auth.utils import hash_password
        from database import get_db

        conn = get_db()
        existing = conn.execute(
            "SELECT id FROM users WHERE email=?",
            (admin_email,),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE users
                SET is_admin=1,
                    subscription_status='active',
                    trial_ends_at=NULL
                WHERE id=?
                """,
                (existing["id"],),
            )
            conn.commit()
            print(f"Admin status refreshed: {admin_email}")
        else:
            conn.execute(
                """
                INSERT INTO users (
                    name, email, password_hash, is_admin,
                    subscription_status, trial_ends_at
                ) VALUES (?, ?, ?, 1, 'active', NULL)
                """,
                (
                    admin_name,
                    admin_email,
                    hash_password(admin_password),
                ),
            )
            conn.commit()
            print(f"Admin created: {admin_email}")
        conn.close()

    # Run once more after the admin row is guaranteed to exist, so the owner's
    # generated editable default copy receives the balanced weights immediately.
    migrate_default_strategy_profiles()

    print(f"Option King AI SaaS Server started | {RELEASE_VERSION}")


app.include_router(auth_router)
app.include_router(recovery_router)
app.include_router(local_gateway_router)
app.include_router(broker_router)
app.include_router(subscription_router)
app.include_router(admin_router)
app.include_router(bot_router)
app.include_router(ai_router)
app.include_router(telegram_router)
app.include_router(user_panel_router)
app.include_router(paper_router)
app.include_router(strategy_router)
app.include_router(strategy_profile_router)
app.include_router(market_router)
app.include_router(backtest_router)


@app.get("/")
def root():
    return {
        "app": "Option King AI SaaS",
        "version": "1.0.0",
        "release": RELEASE_VERSION,
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    try:
        from database import get_db
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {str(exc)}"

    return {
        "status": "healthy",
        "database": db_status,
        "release": RELEASE_VERSION,
    }


from fastapi.responses import FileResponse, HTMLResponse
from html import escape


@app.get("/upstox/callback", response_class=HTMLResponse)
def upstox_callback(code: str = "", state: str = ""):
    safe_code = escape(str(code or ""))
    safe_state = escape(str(state or ""))
    code_html = (
        "<p><b>Authorization Code:</b></p>"
        f"<div style='word-break:break-all;color:#f5c842'>{safe_code}</div>"
        if safe_code
        else (
            "<p>Redirect URL verified. Manual token ke liye "
            "Developer Apps me Generate dabayein.</p>"
        )
    )
    state_html = (
        f"<p style='color:#777'>State: {safe_state}</p>"
        if safe_state
        else ""
    )
    return (
        "<!doctype html><html><head>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Option King AI Upstox</title></head>"
        "<body style='background:#0a0a0f;color:#e8e8f0;"
        "font-family:Arial;padding:24px'>"
        "<div style='max-width:680px;margin:auto;background:#13131f;"
        "border:1px solid #252540;border-radius:18px;padding:22px'>"
        "<h2 style='color:#00d4a0'>✅ Option King AI Upstox Callback</h2>"
        "<p>Upstox redirect successfully receive ho gaya.</p>"
        + code_html
        + state_html
        + "</div></body></html>"
    )


@app.post("/upstox/postback")
def upstox_postback(body: dict = None):
    return {"success": True, "received": True}


@app.get("/admin/panel")
def admin_panel():
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "admin/panel.html")
    )


@app.get("/signup")
def signup_page():
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "signup.html")
    )


@app.get("/join")
def join_page():
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "signup.html")
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8001))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )
