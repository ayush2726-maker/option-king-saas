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
