"""Option King bot package runtime patches."""

# Keep imports fail-closed so local scripts/tests that do not configure broker
# credentials still start normally.
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
