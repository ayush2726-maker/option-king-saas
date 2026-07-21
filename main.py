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
from backtest.live_strategy_consistency_patch import (
    BacktestActiveStrategyMiddleware,
    apply_backtest_live_strategy_patch,
)
from backtest.live_frequency_portfolio_patch import (
    apply_live_frequency_portfolio_patch,
)
from backtest.realism_costs_patch import apply_backtest_realism_costs_patch
from backtest.cost_idempotence_patch import apply_cost_idempotence_patch
from backtest.monthly_job_start_patch import apply_monthly_job_start_patch
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
from bot.entry_quality_v2_patch import apply_entry_quality_v2_patch
from bot.structural_exit_v2_patch import apply_structural_exit_v2_patch
from bot.local_gateway_structural_exit_patch import (
    apply_local_gateway_structural_exit_patch,
)
from bot.expiry_hardlock_one_second_monitor_patch import (
    apply_expiry_hardlock_one_second_monitor_patch,
)
from bot.hero_zero_guard_patch import apply_hero_zero_guard_patch
from bot.manual_exit_patch import apply_manual_exit_patch
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
apply_entry_quality_v2_patch()
apply_structural_exit_v2_patch()
apply_local_gateway_structural_exit_patch()
apply_expiry_hardlock_one_second_monitor_patch()
apply_hero_zero_guard_patch()
apply_manual_exit_patch()
apply_backtest_live_strategy_patch()
apply_live_frequency_portfolio_patch()
apply_backtest_realism_costs_patch()
apply_cost_idempotence_patch()
apply_monthly_job_start_patch()

RELEASE_VERSION = "local-gateway-risk-v2-structural-exit"

app = FastAPI(
    title="Option King AI — SaaS API",
    description="Multi-user F&O trading bot platform",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(BacktestActiveStrategyMiddleware)
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
