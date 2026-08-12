from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import init_db, get_db as open_database
from auth.routes import router as auth_router
from auth.recovery_routes import router as recovery_router, ensure_recovery_schema
from auth.registration_email_middleware import SafeRegistrationEmailVerificationMiddleware
from local_gateway.routes import router as local_gateway_router
from local_gateway.service import ensure_local_gateway_schema
from broker.routes import router as broker_router
from broker.selection import normalize_all_selected_brokers
from broker.selected_broker_control import (
    router as broker_selection_router,
    repair_admin_angel_selection_once,
)
from subscription.routes import router as subscription_router
from admin.routes import router as admin_router
from bot.routes import router as bot_router, ensure_tables as ensure_bot_tables
from bot.trade_live_routes import router as trade_live_router
from telegram.routes import router as telegram_router
from user_panel.routes import router as user_panel_router
from paper.routes import router as paper_router
from strategy.routes import router as strategy_router
from strategy.profile_routes import router as strategy_profile_router
from bot.market_routes import router as market_router
from bot.sector_rotation_routes import router as sector_rotation_router
from bot.ai_routes import router as ai_router
from backtest.routes import router as backtest_router
from backtest.range_routes import router as backtest_range_router
from backtest.live_strategy_consistency_patch import (
    BacktestActiveStrategyMiddleware,
    apply_backtest_live_strategy_patch,
)
from backtest.live_frequency_portfolio_patch import apply_live_frequency_portfolio_patch
from backtest.upstox_historical_key_patch import apply_upstox_historical_key_patch
from backtest.post_loss_reentry_cooldown_patch import apply_backtest_post_loss_reentry_cooldown_patch
from backtest.realism_costs_patch import apply_backtest_realism_costs_patch
from backtest.cost_idempotence_patch import apply_cost_idempotence_patch
from backtest.monthly_job_start_patch import apply_monthly_job_start_patch
from backtest.normal_entry_cutoff_1445_patch import apply_normal_entry_cutoff_1445_patch
from backtest.real_option_premium_patch import prepare_real_option_premium_patch
from backtest.real_option_premium_finalize_patch import finalize_real_option_premium_patch
from bot.score_history_patch import apply_score_history_patch
from bot.upstox_live_candle_patch import apply_upstox_live_candle_patch
from bot.live_scan_history_fallback_patch import apply_live_scan_history_fallback_patch
from bot.default_strategy_patch import (
    apply_default_strategy_patch,
    migrate_default_strategy_profiles,
)
from bot.bullish_balance_cas_guard_patch import (
    apply_balanced_momentum_patch,
    apply_cas_closing_guard_patch,
)
from bot.fresh_entry_guard_patch import apply_fresh_entry_guard_patch
from bot.expiry_entry_diagnostics_patch import apply_expiry_entry_diagnostics_patch
from bot.feed_safety_consistency_patch import apply_feed_safety_consistency_patch
from bot.anti_chase_consistency_v3_patch import apply_anti_chase_consistency_v3_patch
from bot.mandatory_trend_structure_patch import apply_mandatory_trend_structure_patch
from bot.entry_quality_v2_patch import apply_entry_quality_v2_patch
from bot.entry_timing_calibration_patch import apply_entry_timing_calibration_patch
from bot.structural_exit_v2_patch import apply_structural_exit_v2_patch
from bot.expiry_hardlock_one_second_monitor_patch import apply_expiry_hardlock_one_second_monitor_patch
from bot.hero_zero_guard_patch import apply_hero_zero_guard_patch
from bot.manual_exit_patch import apply_manual_exit_patch
from bot.paper_unlimited_observation_patch import apply_paper_unlimited_observation_patch
from bot.post_loss_reentry_guard_patch import apply_post_loss_reentry_guard_patch
from bot.capital_based_sizing_restore_patch import apply_capital_based_sizing_restore_patch
from bot.expectancy_engine_v1_patch import apply_expectancy_engine_v1_patch
from bot.broker_session_reset_patch import apply_broker_session_reset_patch
from bot.signal_history_response_middleware import StrictSignalHistoryMiddleware
from bot.eod_safety_testing_access_patch import (
    TestingFullAccessAndFreshDataMiddleware,
    apply_eod_entry_guard_patch,
    initialize_testing_access_and_cleanup,
)
from bot.consecutive_loss_cooldown_patch import apply_consecutive_loss_cooldown_patch
from bot.active_strategy_score_patch import apply_active_strategy_score_patch
from bot.decision_score_display_consistency_patch import (
    apply_decision_score_display_consistency_patch,
)
from bot.breakeven_4pct_patch import apply_breakeven_4pct_patch
from bot.live_quote_runtime_recovery import (
    TradeLiveRuntimeRecoveryMiddleware,
    apply_live_quote_timestamp_patch,
    recover_persisted_open_trade_engines,
)
from bot.entry_execution_safety_v1_patch import (
    apply_entry_execution_safety_v1_patch,
)
from bot.pullback_continuation_entry_patch import (
    apply_pullback_continuation_entry_patch,
)
from bot.missed_trade_learning_v1 import (
    apply_missed_trade_learning_patch,
)
from bot.admin_morning_trade_cleanup_20260804 import (
    delete_admin_morning_paper_trades_20260804,
)
import os

# Preserve the proven runtime patch order. The sector-rotation API is display-only
# and is registered as a separate router below; it never wraps this trade engine.
apply_broker_session_reset_patch()
apply_score_history_patch()
apply_upstox_live_candle_patch()
apply_live_scan_history_fallback_patch()
apply_default_strategy_patch()
# Correct candle-direction asymmetry before all final entry/score guards attach.
apply_balanced_momentum_patch()
apply_fresh_entry_guard_patch()
apply_expiry_entry_diagnostics_patch()
apply_feed_safety_consistency_patch()
apply_anti_chase_consistency_v3_patch()
apply_mandatory_trend_structure_patch()
apply_entry_quality_v2_patch()
apply_entry_timing_calibration_patch()
prepare_real_option_premium_patch()
apply_structural_exit_v2_patch()
apply_expiry_hardlock_one_second_monitor_patch()
apply_hero_zero_guard_patch()
apply_manual_exit_patch()
apply_paper_unlimited_observation_patch()
apply_post_loss_reentry_guard_patch()
apply_backtest_live_strategy_patch()
apply_live_frequency_portfolio_patch()
apply_upstox_historical_key_patch()
apply_backtest_post_loss_reentry_cooldown_patch()
apply_backtest_realism_costs_patch()
apply_cost_idempotence_patch()
apply_monthly_job_start_patch()
apply_normal_entry_cutoff_1445_patch()
apply_capital_based_sizing_restore_patch()
apply_expectancy_engine_v1_patch()
apply_breakeven_4pct_patch()
finalize_real_option_premium_patch()
apply_eod_entry_guard_patch()
apply_consecutive_loss_cooldown_patch()
apply_active_strategy_score_patch()
apply_decision_score_display_consistency_patch()
apply_live_quote_timestamp_patch()
# Final entry execution layer: fresh data + option premium confirmation + audit.
apply_entry_execution_safety_v1_patch()
# Final date-aware exit layer: from 03-Aug-2026 close before the 15:15 CAS.
apply_cas_closing_guard_patch()
# Final scan-selection layer: remember mature extended setups and release them
# only after a safe EMA pullback plus same-direction continuation candle.
apply_pullback_continuation_entry_patch()
# Final observability/learning layer. It records post-decision missed setups and
# evaluates them in a background shadow worker; it cannot mutate scans/orders.
apply_missed_trade_learning_patch()

RELEASE_VERSION = "retire-ema-orb-trigger-blocks-v1"

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
app.add_middleware(TestingFullAccessAndFreshDataMiddleware)
app.add_middleware(TradeLiveRuntimeRecoveryMiddleware)
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

    # Complete the bot schema once when the process starts. /bot/signal is a
    # high-frequency read endpoint and must not retry every CREATE/ALTER on each
    # mobile refresh.
    bot_conn = open_database()
    try:
        ensure_bot_tables(bot_conn)
    finally:
        bot_conn.close()

    ensure_recovery_schema()
    ensure_local_gateway_schema()

    from database import init_bot_status_table
    init_bot_status_table()

    testing_init = initialize_testing_access_and_cleanup()
    print(
        "Testing access/EOD cleanup | "
        f"users={testing_init['testing_access_users_updated']} | "
        f"removed={testing_init['invalid_eod_paper_trades_removed']}"
    )

    repaired = normalize_all_selected_brokers()
    if repaired:
        print(f"Broker selection normalized for {repaired} user(s)")

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

    morning_cleanup = delete_admin_morning_paper_trades_20260804()
    print(
        "Admin 04-Aug 09:15-09:38 permanent cleanup | "
        f"removed={morning_cleanup['removed']} | "
        f"users={morning_cleanup['affected_users']} | "
        f"already_applied={morning_cleanup['already_applied']}"
    )

    admin_broker_repaired = repair_admin_angel_selection_once()
    if admin_broker_repaired:
        print(
            "Admin selected broker repaired to Angel One for "
            f"{admin_broker_repaired} user(s)"
        )

    migrate_default_strategy_profiles()

    quote_recovery = recover_persisted_open_trade_engines()
    print(
        "Live quote runtime recovery | "
        f"eligible={quote_recovery['eligible_users']} | "
        f"running={quote_recovery['running_or_started']} | "
        f"failed={len(quote_recovery['failed'])}"
    )

    print(f"Option King AI SaaS Server started | {RELEASE_VERSION}")


app.include_router(auth_router)
app.include_router(recovery_router)
app.include_router(broker_router)
app.include_router(broker_selection_router)
app.include_router(local_gateway_router)
app.include_router(subscription_router)
app.include_router(admin_router)
app.include_router(bot_router)
app.include_router(trade_live_router)
app.include_router(telegram_router)
app.include_router(user_panel_router)
app.include_router(paper_router)
app.include_router(strategy_router)
app.include_router(strategy_profile_router)
app.include_router(market_router)
app.include_router(sector_rotation_router)
app.include_router(ai_router)
app.include_router(backtest_router)
app.include_router(backtest_range_router)


@app.get("/")
def root():
    return {"message": "Option King AI SaaS API running"}


@app.get("/health")
def health():
    return {"status": "ok", "version": RELEASE_VERSION}
