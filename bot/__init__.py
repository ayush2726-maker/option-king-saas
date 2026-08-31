"""Option King bot package runtime patches."""

# Keep imports fail-closed so local scripts/tests that do not configure broker
# credentials still start normally.
try:
    import sitecustomize as _angel_search_scrip_compat
    _angel_search_scrip_compat._install()
except Exception: pass
try:
    from bot.angel_pcr_recovery_patch import apply_angel_pcr_recovery_patch
    apply_angel_pcr_recovery_patch()
except Exception: pass
try:
    from bot.post_loss_reentry_guard_patch import apply_post_loss_reentry_guard_patch
    apply_post_loss_reentry_guard_patch()
except Exception: pass
try:
    from bot.entry_direction_confirmation_patch import apply_entry_direction_confirmation_patch
    apply_entry_direction_confirmation_patch()
except Exception: pass
try:
    from bot.selected_broker_ai_summary_guard_patch import apply_selected_broker_ai_summary_guard_patch
    apply_selected_broker_ai_summary_guard_patch()
except Exception: pass
try:
    from bot.orb_gap_neutral_scoring_patch import apply_orb_gap_neutral_scoring_patch
    apply_orb_gap_neutral_scoring_patch()
except Exception: pass
try:
    from bot.auto_entry_attempt_diagnostics_patch import apply_auto_entry_attempt_diagnostics_patch
    apply_auto_entry_attempt_diagnostics_patch()
except Exception: pass
try:
    from bot.orb_session_backfill_patch import apply_orb_session_backfill_patch
    apply_orb_session_backfill_patch()
except Exception: pass
try:
    from bot.replay_orb_runtime_patch import apply_replay_orb_runtime_patch
    apply_replay_orb_runtime_patch()
except Exception: pass
try:
    from bot.final_contract_and_quote_safety_patch import apply_final_contract_and_quote_safety_patch
    apply_final_contract_and_quote_safety_patch()
except Exception: pass
try:
    from bot.far_expiry_cleanup_scheduler import schedule_far_expiry_cleanup
    schedule_far_expiry_cleanup()
except Exception: pass
try:
    from bot.admin_cleanup_report_patch import apply_admin_cleanup_report_patch
    apply_admin_cleanup_report_patch()
except Exception: pass
try:
    from bot.market_knowledge_brain_v1 import schedule_market_knowledge_brain_patch
    schedule_market_knowledge_brain_patch()
except Exception: pass
try:
    from bot.sector_rotation_ai_training_v1 import schedule_sector_rotation_ai_training_patch
    schedule_sector_rotation_ai_training_patch()
except Exception: pass
try:
    from bot.upstox_sector_change_fix_v1 import schedule_upstox_sector_change_fix
    schedule_upstox_sector_change_fix()
except Exception: pass
try:
    from bot.supertrend_completed_candle_consistency_patch import apply_supertrend_completed_candle_consistency_patch
    apply_supertrend_completed_candle_consistency_patch()
except Exception: pass
try:
    from bot.supertrend_replay_final_patch import apply_supertrend_replay_final_patch
    apply_supertrend_replay_final_patch()
except Exception: pass

try:
    from bot.upstox_nearest_expiry_fix_v1 import apply_upstox_nearest_expiry_fix
    apply_upstox_nearest_expiry_fix()
except Exception: pass

try:
    from bot.ema_anti_chase_observation_only_patch import apply_ema_anti_chase_observation_only_patch
    apply_ema_anti_chase_observation_only_patch()
except Exception: pass

try:
    from bot.adaptive_learning_v3_patch import apply_adaptive_learning_v3_patch
    apply_adaptive_learning_v3_patch()
except Exception: pass
try:
    from bot.option_premium_learning_v3_patch import apply_option_premium_learning_v3_patch
    apply_option_premium_learning_v3_patch()
except Exception: pass
try:
    from bot.market_mechanics_learning_v4_patch import apply_market_mechanics_learning_v4_patch
    apply_market_mechanics_learning_v4_patch()
except Exception: pass
try:
    from bot.baseline_setup_training_v5_patch import apply_baseline_setup_training_v5_patch
    apply_baseline_setup_training_v5_patch()
except Exception: pass
try:
    from bot.gainzalgo_shadow_v1 import apply_gainzalgo_shadow_v1_patch
    apply_gainzalgo_shadow_v1_patch()
except Exception: pass
try:
    from bot.free_regime_indicators_v1 import apply_free_regime_indicators_v1_patch
    apply_free_regime_indicators_v1_patch()
except Exception: pass
try:
    from bot.adaptive_accuracy_v6_patch import apply_adaptive_accuracy_v6_patch
    apply_adaptive_accuracy_v6_patch()
except Exception: pass
try:
    from bot.adaptive_runtime_binding_v3 import apply_adaptive_runtime_binding_v3
    apply_adaptive_runtime_binding_v3()
except Exception: pass

try:
    from bot import opening_orb_loss_circuit_patch as _opening_loss_circuit
    def _no_global_loss_block(conn, user_id):
        return None
    _opening_loss_circuit._global_block = _no_global_loss_block
    _opening_loss_circuit.GLOBAL_LOSS_BLOCK_DISABLED = True
except Exception: pass
try:
    from bot.execution_reason_visibility_patch import apply_execution_reason_visibility_patch
    apply_execution_reason_visibility_patch()
except Exception: pass
try:
    from bot.regime_accuracy_confirmation_patch import apply_regime_accuracy_confirmation_patch
    apply_regime_accuracy_confirmation_patch()
except Exception: pass
try:
    from bot.risk_control_v2_patch import apply_risk_control_v2_patch
    apply_risk_control_v2_patch()
except Exception: pass
try:
    from bot.breakeven_4pct_patch import apply_breakeven_4pct_patch
    apply_breakeven_4pct_patch()
except Exception: pass
try:
    from bot.fast_opposite_trend_exit_patch import apply_fast_opposite_trend_exit_patch
    apply_fast_opposite_trend_exit_patch()
except Exception: pass
try:
    from bot.market_close_1540_cutoff_patch import apply_market_close_1540_cutoff_patch
    apply_market_close_1540_cutoff_patch()
except Exception: pass
try:
    from bot.shared_nonlive_data_feed_patch import apply_shared_nonlive_data_feed_patch
    apply_shared_nonlive_data_feed_patch()
except Exception: pass
try:
    from bot.live_quote_broker_relogin_patch import schedule_live_quote_broker_relogin_patch
    schedule_live_quote_broker_relogin_patch()
except Exception: pass
try:
    from bot.direct_stale_quote_recovery_v1 import schedule_direct_stale_quote_recovery
    schedule_direct_stale_quote_recovery()
except Exception: pass
try:
    from bot.quote_success_timestamp_v1 import schedule_quote_success_timestamp_patch
    schedule_quote_success_timestamp_patch()
except Exception: pass
try:
    from bot.upstox_open_quote_batch_patch import schedule_upstox_open_quote_batch_patch
    schedule_upstox_open_quote_batch_patch()
except Exception: pass
try:
    from bot.live_broker_protection_v1 import schedule_live_broker_protection
    schedule_live_broker_protection()
except Exception: pass
try:
    from bot.quote_freshness_harmonizer_v1 import schedule_quote_freshness_harmonizer
    schedule_quote_freshness_harmonizer()
except Exception: pass

try:
    from bot.paper_quote_multi_broker_recovery_v1 import schedule_paper_quote_multi_broker_recovery
    schedule_paper_quote_multi_broker_recovery()
except Exception: pass

try:
    from bot.paper_quote_authority_v2 import schedule_paper_quote_authority_v2
    schedule_paper_quote_authority_v2()
except Exception: pass

# If Upstox market-quote LTP still stalls, recover the exact strike/side from the
# official option-chain endpoint and feed that LTP through the same exit/trailing
# evaluator. This is PAPER-only and does not change entry/risk/strategy rules.
try:
    from bot.upstox_option_chain_quote_fallback_v1 import schedule_upstox_option_chain_quote_fallback
    schedule_upstox_option_chain_quote_fallback()
except Exception: pass

# Make the final structural runtime use the same safer exit ordering in PAPER
# and LIVE. The wrapper tightens confirmed structural loss exit to -0.25R and
# re-applies fast opposite-trend exit after structural_exit_v2 installs.
try:
    from bot.structural_exit_runtime_order_fix_v1 import install_structural_exit_runtime_order_fix_v1
    install_structural_exit_runtime_order_fix_v1()
except Exception: pass
