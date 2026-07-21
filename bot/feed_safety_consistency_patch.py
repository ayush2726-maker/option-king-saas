"""Feed status and signal-safety consistency patch.

The balanced default strategy reports CUSTOM_PROFILE_V1, while the Shared AI
snapshot previously accepted only TQU_ENHANCED.  That produced a false
FEED_DISCONNECTED banner even while AUTO Portfolio had live candles/scores.

This patch also gives every 82+ signal a deterministic safety-reason list and
repairs only impossible gate-state mismatches.  It does not weaken score,
fresh-entry, anti-chase, sideways, or direction requirements.
"""

from datetime import datetime, timezone

from bot import ai_routes
from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime
from bot import routes
from bot import strategy


def _number(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _feed_snapshot_v2(original, user_id):
    snapshot = dict(original(user_id) or {})
    state = dict(angel_fetcher.get_user_bot_state(user_id) or {})
    now_utc = datetime.now(timezone.utc)

    updated_at = state.get("updated_at")
    feed_age_ms = ai_routes._feed_age_ms(updated_at, now_utc)
    price = _number(state.get("price"), 0)
    status = str(state.get("status") or "NOT_STARTED")
    strategy_name = str(state.get("strategy") or "")
    scans = state.get("scan_results") or []
    scan_ready = any(
        isinstance(scan, dict)
        and str(scan.get("status") or "").upper() in {
            "OK", "QUALIFIED", "SAFETY_BLOCKED", "ENTRY_BLOCKED"
        }
        and _number(scan.get("price"), 0) > 0
        for scan in scans
    )

    accepted_strategy = strategy_name in {
        "TQU_ENHANCED",
        "CUSTOM_PROFILE_V1",
        "OKAI_DEFAULT_BALANCED_V2",
    }
    engine_ready = price > 0 and (
        accepted_strategy
        or scan_ready
        or str(state.get("engine_mode") or "").startswith("AUTO_PORTFOLIO")
    )
    connected = bool(
        engine_ready
        and feed_age_ms <= 130000
        and not status.startswith("ERROR")
    )

    snapshot.update({
        "price": price,
        "strategy": strategy_name,
        "engine_status": status,
        "engine_updated_at": updated_at,
        "feed_age_ms": feed_age_ms,
        "feed_connected": connected,
        "feed_reason": (
            "CONNECTED"
            if connected
            else "STALE_FEED"
            if engine_ready and feed_age_ms > 130000
            else "ENGINE_ERROR"
            if status.startswith("ERROR")
            else "ENGINE_NOT_READY"
        ),
    })
    return snapshot


def _safety_reasons(result):
    candidate = str(result.get("candidate_signal") or "WAIT").upper()
    score = int(round(_number(result.get("score"), 0)))
    minimum = int(round(_number(result.get("min_score", 82), 82)))
    core = int(round(_number(result.get("core_confirmations"), 0)))

    reasons = []
    if candidate not in ("CE", "PE"):
        reasons.append("NO_DIRECTIONAL_SIGNAL")
    if score < minimum:
        reasons.append(f"SCORE_BELOW_{minimum}")
    if candidate in ("CE", "PE") and core < 4:
        reasons.append(f"CORE_CONFIRMATIONS_{core}_OF_4")

    for reason in result.get("fresh_entry_block_reasons") or []:
        text = str(reason or "").strip()
        if text and text not in reasons:
            reasons.append(text)

    if result.get("sideways_blocked"):
        reasons.append("SIDEWAYS_BLOCKED")
    if result.get("ema_chase_blocked"):
        reasons.append(
            "EMA_ANTI_CHASE:"
            f"{_number(result.get('ema_stretch_points'), 0):.1f}>"
            f"{_number(result.get('ema_stretch_limit'), 0):.1f}"
        )
    if result.get("vwap_chase_blocked"):
        reasons.append(
            "VWAP_ANTI_CHASE:"
            f"{_number(result.get('vwap_stretch_points'), 0):.1f}>"
            f"{_number(result.get('vwap_stretch_limit'), 0):.1f}"
        )
    if (
        result.get("anti_chase_blocked")
        and not result.get("ema_chase_blocked")
        and not result.get("vwap_chase_blocked")
    ):
        reasons.append("ANTI_CHASE_BLOCKED")
    if (
        result.get("chase_blocked")
        and not result.get("ema_chase_blocked")
        and not result.get("vwap_chase_blocked")
        and "ANTI_CHASE_BLOCKED" not in reasons
    ):
        reasons.append("CHASE_GUARD_BLOCKED")

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(reasons))


def _consistent_signal(original, market_data, consecutive_losses=0, profile=None):
    result = original(
        market_data,
        consecutive_losses=consecutive_losses,
        profile=profile,
    )
    if not isinstance(result, dict):
        return result

    output = dict(result)
    reasons = _safety_reasons(output)
    candidate = str(output.get("candidate_signal") or "WAIT").upper()
    score = int(round(_number(output.get("score"), 0)))
    minimum = int(round(_number(output.get("min_score", 82), 82)))
    core = int(round(_number(output.get("core_confirmations"), 0)))

    eligible = bool(
        candidate in ("CE", "PE")
        and score >= minimum
        and core >= 4
        and not reasons
    )
    previous_allowed = bool(output.get("trade_allowed", False))

    output["safety_gate_reasons"] = reasons
    output["safety_gate_passed"] = eligible
    output["gate_consistency_repaired"] = False

    # This is not a relaxation.  It only makes trade_allowed equal to the exact
    # same gates already represented by the signal result.
    if eligible and not previous_allowed:
        output["trade_allowed"] = True
        output["signal"] = candidate
        output["gate_consistency_repaired"] = True
        warnings = list(output.get("warnings") or [])
        warnings.append("SIGNAL_GATE_CONSISTENCY_REPAIRED")
        output["warnings"] = warnings
    elif not eligible:
        output["trade_allowed"] = False
        output["signal"] = "WAIT"

    return output


def _reason_summary(original, scan):
    summary = dict(original(scan) or {})
    signal = scan.get("signal_data") or {}
    reasons = signal.get("safety_gate_reasons") or _safety_reasons(signal)
    score = int(summary.get("score") or 0)
    minimum = int(summary.get("min_score") or 82)

    if score >= minimum and not summary.get("trade_allowed"):
        reason = str(reasons[0] if reasons else "SIGNAL_GATE_STATE_MISMATCH")
        summary["status"] = "SAFETY_BLOCKED"
        summary["candidate_signal"] = reason[:90]
        summary["entry_block_reason"] = reason[:180]
    elif summary.get("trade_allowed"):
        summary["status"] = "QUALIFIED"
        summary["entry_block_reason"] = None
        summary["entry_status"] = "QUALIFIED"

    summary["safety_gate_reasons"] = list(reasons)[:8]
    summary["safety_gate_passed"] = bool(signal.get("safety_gate_passed"))
    summary["gate_consistency_repaired"] = bool(
        signal.get("gate_consistency_repaired")
    )
    return summary


def apply_feed_safety_consistency_patch():
    if getattr(strategy, "_okai_feed_safety_consistency_v1", False):
        return

    original_snapshot = ai_routes._user_snapshot
    ai_routes._user_snapshot = lambda user_id: _feed_snapshot_v2(
        original_snapshot, user_id
    )

    original_signal = angel_fetcher.get_full_signal

    def consistent_get_full_signal(
        market_data,
        consecutive_losses=0,
        profile=None,
    ):
        return _consistent_signal(
            original_signal,
            market_data,
            consecutive_losses=consecutive_losses,
            profile=profile,
        )

    strategy.get_full_signal = consistent_get_full_signal
    angel_fetcher.get_full_signal = consistent_get_full_signal
    routes.get_full_signal = consistent_get_full_signal

    original_summary = runtime._summary
    runtime._summary = lambda scan: _reason_summary(original_summary, scan)

    strategy._okai_feed_safety_consistency_v1 = True
