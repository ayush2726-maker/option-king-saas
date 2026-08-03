"""Option King bot package runtime patches."""

# Keep imports fail-closed so local scripts/tests that do not configure broker
# credentials still start normally.
try:
    import sitecustomize as _angel_search_scrip_compat

    _angel_search_scrip_compat._install()
except Exception:
    pass

try:
    from bot.angel_pcr_recovery_patch import apply_angel_pcr_recovery_patch

    apply_angel_pcr_recovery_patch()
except Exception:
    pass

try:
    from bot.post_loss_reentry_guard_patch import apply_post_loss_reentry_guard_patch

    apply_post_loss_reentry_guard_patch()
except Exception:
    pass

try:
    from bot.entry_direction_confirmation_patch import apply_entry_direction_confirmation_patch

    apply_entry_direction_confirmation_patch()
except Exception:
    pass

try:
    from bot.selected_broker_ai_summary_guard_patch import (
        apply_selected_broker_ai_summary_guard_patch,
    )

    apply_selected_broker_ai_summary_guard_patch()
except Exception:
    pass

try:
    from bot.orb_gap_neutral_scoring_patch import (
        apply_orb_gap_neutral_scoring_patch,
    )

    apply_orb_gap_neutral_scoring_patch()
except Exception:
    pass

try:
    from bot.auto_entry_attempt_diagnostics_patch import (
        apply_auto_entry_attempt_diagnostics_patch,
    )

    apply_auto_entry_attempt_diagnostics_patch()
except Exception:
    pass

try:
    from bot.orb_session_backfill_patch import (
        apply_orb_session_backfill_patch,
    )

    apply_orb_session_backfill_patch()
except Exception:
    pass

try:
    from bot.replay_orb_runtime_patch import (
        apply_replay_orb_runtime_patch,
    )

    apply_replay_orb_runtime_patch()
except Exception:
    pass

try:
    from bot.final_contract_and_quote_safety_patch import (
        apply_final_contract_and_quote_safety_patch,
    )

    apply_final_contract_and_quote_safety_patch()
except Exception:
    pass

try:
    from bot.far_expiry_cleanup_scheduler import schedule_far_expiry_cleanup

    schedule_far_expiry_cleanup()
except Exception:
    pass

try:
    from bot.admin_cleanup_report_patch import apply_admin_cleanup_report_patch

    apply_admin_cleanup_report_patch()
except Exception:
    pass

try:
    from bot.market_knowledge_brain_v1 import (
        schedule_market_knowledge_brain_patch,
    )

    schedule_market_knowledge_brain_patch()
except Exception:
    pass

try:
    from bot.sector_rotation_ai_training_v1 import (
        schedule_sector_rotation_ai_training_patch,
    )

    schedule_sector_rotation_ai_training_patch()
except Exception:
    pass

try:
    from bot.upstox_sector_change_fix_v1 import (
        schedule_upstox_sector_change_fix,
    )

    schedule_upstox_sector_change_fix()
except Exception:
    pass

try:
    from bot.supertrend_completed_candle_consistency_patch import (
        apply_supertrend_completed_candle_consistency_patch,
    )

    apply_supertrend_completed_candle_consistency_patch()
except Exception:
    pass
