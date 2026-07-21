"""Feed status and signal-safety consistency patch.

The balanced default strategy reports CUSTOM_PROFILE_V1, while the Shared AI
snapshot previously accepted only TQU_ENHANCED. That produced a false
FEED_DISCONNECTED banner even while AUTO Portfolio had live candles/scores.

Some replay/live scan paths also returned a valid score but omitted
`core_confirmations`. Treating a missing field as zero falsely blocked 100/82
setups. This patch derives the five directional checks from the same market
snapshot before the AUTO selector runs. It keeps score, fresh-entry,
anti-chase, sideways and direction gates intact.
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


def _directional_snapshot(result, market_data):
    market = dict(market_data or {})
    candidate = str(result.get("candidate_signal") or "WAIT").upper()

    price = _number(market.get("price"), 0)
    vwap = _number(market.get("vwap"), price)
    ema9 = _number(market.get("ema9"), price)
    ema21 = _number(market.get("ema21"), price)
    atr = max(0.01, _number(market.get("atr"), 0.01))
    supertrend = str(market.get("supertrend_dir") or "NEUTRAL").upper()
    trend = str(market.get("trend") or "SIDEWAYS").upper()
    orb_high = _number(market.get("orb_high"), 0)
    orb_low = _number(market.get("orb_low"), 0)
    c1_bull = bool(market.get("c1_bullish", False))
    c2_bull = bool(market.get("c2_bullish", False))
    vwap_fallback = bool(market.get("vwap_fallback_used", False))

    ce_checks = {
        "vwap": price > vwap,
        "supertrend": supertrend == "UP",
        "ema_trend": ema9 > ema21 and trend == "UPTREND",
        "orb": orb_high > 0 and price > orb_high + 5,
        "momentum": c1_bull and c2_bull,
    }
    pe_checks = {
        "vwap": price < vwap,
        "supertrend": supertrend == "DOWN",
        "ema_trend": ema9 < ema21 and trend == "DOWNTREND",
        "orb": orb_low > 0 and price < orb_low - 5,
        "momentum": (not c1_bull) and (not c2_bull),
    }

    if candidate == "CE":
        checks = ce_checks
        current_aligned = c2_bull
        two_candle_run = c1_bull and c2_bull
        orb_extension_atr = (
            max(0.0, price - (orb_high + 5)) / atr if orb_high > 0 else 0.0
        )
    elif candidate == "PE":
        checks = pe_checks
        current_aligned = not c2_bull
        two_candle_run = (not c1_bull) and (not c2_bull)
        orb_extension_atr = (
            max(0.0, (orb_low - 5) - price) / atr if orb_low > 0 else 0.0
        )
    else:
        checks = {}
        current_aligned = False
        two_candle_run = False
        orb_extension_atr = 0.0

    core = sum(1 for passed in checks.values() if passed)
    ema_distance_atr = abs(price - ema9) / atr
    vwap_distance_atr = abs(price - vwap) / atr

    fresh_reasons = []
    score = int(round(_number(result.get("score"), 0)))
    minimum = int(round(_number(result.get("min_score", 82), 82)))
    if candidate in ("CE", "PE") and score >= minimum:
        if core < 4:
            fresh_reasons.append(f"CORE_CONFIRMATIONS_{core}_OF_4")
        if not current_aligned:
            fresh_reasons.append("REVERSAL_CANDLE_AT_ENTRY")
        if ema_distance_atr > 0.95:
            fresh_reasons.append("EMA_EXTENSION_OVER_0.95_ATR")
        if orb_extension_atr > 1.35:
            fresh_reasons.append("ORB_EXTENSION_OVER_1.35_ATR")
        if (not vwap_fallback) and vwap_distance_atr > 2.20:
            fresh_reasons.append("VWAP_EXTENSION_OVER_2.20_ATR")
        if two_candle_run and ema_distance_atr > 0.80 and orb_extension_atr > 0.90:
            fresh_reasons.append("LATE_TWO_CANDLE_EXHAUSTION")

    return {
        "core": core,
        "checks": checks,
        "fresh_reasons": fresh_reasons,
        "ema_distance_atr": round(ema_distance_atr, 2),
        "vwap_distance_atr": round(vwap_distance_atr, 2),
        "orb_extension_atr": round(orb_extension_atr, 2),
    }


def _safety_reasons(result, market_data=None):
    candidate = str(result.get("candidate_signal") or "WAIT").upper()
    score = int(round(_number(result.get("score"), 0)))
    minimum = int(round(_number(result.get("min_score", 82), 82)))

    derived = _directional_snapshot(result, market_data) if market_data else None
    if result.get("core_confirmations") is None:
        core = int((derived or {}).get("core", 0))
    else:
        core = int(round(_number(result.get("core_confirmations"), 0)))

    reasons = []
    if candidate not in ("CE", "PE"):
        reasons.append("NO_DIRECTIONAL_SIGNAL")
    if score < minimum:
        reasons.append(f"SCORE_BELOW_{minimum}")
    if candidate in ("CE", "PE") and core < 4:
        reasons.append(f"CORE_CONFIRMATIONS_{core}_OF_4")

    fresh = result.get("fresh_entry_block_reasons")
    if fresh is None and derived:
        fresh = derived.get("fresh_reasons") or []
    for reason in fresh or []:
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

    return list(dict.fromkeys(reasons))


def _normalize_signal(result, market_data):
    if not isinstance(result, dict):
        return result

    output = dict(result)
    derived = _directional_snapshot(output, market_data)
    output["core_confirmations"] = derived["core"]
    output["directional_checks"] = derived["checks"]
    output["ema_distance_atr"] = derived["ema_distance_atr"]
    output["vwap_distance_atr"] = derived["vwap_distance_atr"]
    output["orb_extension_atr"] = derived["orb_extension_atr"]

    existing_fresh = output.get("fresh_entry_block_reasons")
    if existing_fresh is None:
        output["fresh_entry_block_reasons"] = derived["fresh_reasons"]
        output["fresh_entry_ok"] = not derived["fresh_reasons"]

    reasons = _safety_reasons(output, market_data)
    candidate = str(output.get("candidate_signal") or "WAIT").upper()
    score = int(round(_number(output.get("score"), 0)))
    minimum = int(round(_number(output.get("min_score", 82), 82)))

    eligible = bool(
        candidate in ("CE", "PE")
        and score >= minimum
        and derived["core"] >= 4
        and not reasons
    )
    previous_allowed = bool(output.get("trade_allowed", False))

    output["safety_gate_reasons"] = reasons
    output["safety_gate_passed"] = eligible
    output["gate_consistency_repaired"] = False

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


def _consistent_signal(original, market_data, consecutive_losses=0, profile=None):
    result = original(
        market_data,
        consecutive_losses=consecutive_losses,
        profile=profile,
    )
    return _normalize_signal(result, market_data)


def _repair_scan(scan):
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan
    market = scan.get("market_data") or {}
    signal = scan.get("signal_data") or {}
    scan["signal_data"] = _normalize_signal(signal, market)
    scan["market_data"]["signal"] = scan["signal_data"].get("signal", "WAIT")
    scan["market_data"]["signal_score"] = scan["signal_data"].get("score", 0)
    return scan


def _reason_summary(original, scan):
    summary = dict(original(scan) or {})
    signal = scan.get("signal_data") or {}
    market = scan.get("market_data") or {}
    reasons = signal.get("safety_gate_reasons") or _safety_reasons(signal, market)
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

    summary["core_confirmations"] = signal.get("core_confirmations")
    summary["directional_checks"] = signal.get("directional_checks")
    summary["safety_gate_reasons"] = list(reasons)[:8]
    summary["safety_gate_passed"] = bool(signal.get("safety_gate_passed"))
    summary["gate_consistency_repaired"] = bool(signal.get("gate_consistency_repaired"))
    return summary


def apply_feed_safety_consistency_patch():
    if getattr(strategy, "_okai_feed_safety_consistency_v2", False):
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

    # Repair every AUTO scan before _best_candidate evaluates trade_allowed.
    original_scan_angel = runtime._scan_angel
    original_scan_multi = runtime._scan_multi

    def scan_angel_consistent(*args, **kwargs):
        return [_repair_scan(scan) for scan in original_scan_angel(*args, **kwargs)]

    def scan_multi_consistent(*args, **kwargs):
        return [_repair_scan(scan) for scan in original_scan_multi(*args, **kwargs)]

    runtime._scan_angel = scan_angel_consistent
    runtime._scan_multi = scan_multi_consistent

    original_summary = runtime._summary
    runtime._summary = lambda scan: _reason_summary(original_summary, scan)

    strategy._okai_feed_safety_consistency_v1 = True
    strategy._okai_feed_safety_consistency_v2 = True
