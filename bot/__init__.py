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

try:
    from bot.supertrend_replay_final_patch import (
        apply_supertrend_replay_final_patch,
    )

    apply_supertrend_replay_final_patch()
except Exception:
    pass

# V3 keeps AI shadow-only but makes learning richer: regime/context interaction
# features, news/option/base agreement and conflict, volatility/liquidity context,
# plus chronological inner-validation hyperparameter selection. Missed-profit
# and avoided-loss outcomes already flow into the same training dataset.
try:
    from bot.adaptive_learning_v3_patch import apply_adaptive_learning_v3_patch

    apply_adaptive_learning_v3_patch()
except Exception:
    pass

# Teach new snapshots the actual option premium/Greeks context available at the
# decision instant. This remains leakage-safe and shadow-only.
try:
    from bot.option_premium_learning_v3_patch import (
        apply_option_premium_learning_v3_patch,
    )

    apply_option_premium_learning_v3_patch()
except Exception:
    pass

# If another startup patch imported Advanced AI earlier, rebind its local
# feature_vector reference to the final V3 builder.
try:
    from bot.adaptive_runtime_binding_v3 import apply_adaptive_runtime_binding_v3

    apply_adaptive_runtime_binding_v3()
except Exception:
    pass

# Do not let two losses in any instruments freeze every otherwise-qualified
# setup for 15 minutes. Keep the safer same-index + same-side cooldown intact so
# an exited losing trade is not immediately reopened in the same direction.
# opening_orb_loss_circuit_patch resolves _global_block from its module globals
# at call time, so this remains effective when main.py installs that final guard.
try:
    from bot import opening_orb_loss_circuit_patch as _opening_loss_circuit

    def _no_global_loss_block(conn, user_id):
        return None

    _opening_loss_circuit._global_block = _no_global_loss_block
    _opening_loss_circuit.GLOBAL_LOSS_BLOCK_DISABLED = True
except Exception:
    pass

# Mirror the concrete runtime entry failure (contract/LTP/sizing/cooldown/order,
# etc.) into the warning stream that the mobile Final Decision card already
# renders. This changes diagnostics only, never entry logic.
try:
    from bot.execution_reason_visibility_patch import (
        apply_execution_reason_visibility_patch,
    )

    apply_execution_reason_visibility_patch()
except Exception:
    pass
