from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import init_db
from auth.routes import router as auth_router
from broker.routes import router as broker_router
from subscription.routes import router as subscription_router
from admin.routes import router as admin_router
from bot.routes import router as bot_router
from telegram.routes import router as telegram_router
from user_panel.routes import router as user_panel_router
from paper.routes import router as paper_router

from strategy.routes import router as strategy_router
from bot.market_routes import router as market_router
from bot.ai_routes import router as ai_router
from backtest.routes import router as backtest_router
import sqlite3
import os

# ─── App Setup ────────────────────────────────────────────────────

app = FastAPI(
    title="Option King AI — SaaS API",
    description="Multi-user F&O trading bot platform",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS — allow mobile app and web
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# ─── Startup ──────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()
    from database import init_bot_status_table
    init_bot_status_table()

    # Auto-create first admin if not exists
    # Change these credentials!
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@optionking.ai")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Admin@123")
    ADMIN_NAME = os.getenv("ADMIN_NAME", "Ayush")

    from auth.utils import hash_password
    from database import get_db
    from datetime import datetime, timedelta

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)
    ).fetchone()

    if not existing:
        trial_ends = (datetime.utcnow() + timedelta(days=36500)).isoformat()  # 100 years
        conn.execute(
            """INSERT INTO users (name, email, password_hash, is_admin, subscription_status, trial_ends_at)
               VALUES (?, ?, ?, 1, 'active', ?)""",
            (ADMIN_NAME, ADMIN_EMAIL, hash_password(ADMIN_PASSWORD), trial_ends)
        )
        conn.commit()
        print(f"✅ Admin created: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    conn.close()

    print("🚀 Option King AI SaaS Server started!")


# ─── Routers ──────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(broker_router)
app.include_router(subscription_router)
app.include_router(admin_router)
app.include_router(bot_router)
app.include_router(ai_router)
app.include_router(telegram_router)
app.include_router(user_panel_router)
app.include_router(paper_router)

app.include_router(strategy_router)
app.include_router(market_router)
app.include_router(backtest_router)


# ─── Health Check ─────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "app": "Option King AI SaaS",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs"
    }


@app.get("/health")
def health():
    try:
        from database import get_db
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {str(e)}"

    return {
        "status": "healthy",
        "database": db_status
    }


# ─── Run ──────────────────────────────────────────────────────────


from fastapi.responses import FileResponse
import os

@app.get("/admin/panel")
def admin_panel():
    return FileResponse(os.path.join(os.path.dirname(__file__), "admin/panel.html"))


@app.get("/signup")
def signup_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "signup.html"))


@app.get("/join")
def join_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "signup.html"))
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
