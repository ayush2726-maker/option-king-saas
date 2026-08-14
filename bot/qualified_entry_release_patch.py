"""Release score-qualified entries from duplicate directional vetoes.

OKAI Default 82 already scores VWAP, Supertrend, EMA, ORB, momentum, ADX,
volume and real 5-minute MTF. Requiring some of those same components again as
independent hard gates made an 82+ decision display as qualified while AUTO
still returned WAIT.

This patch removes only duplicated/advisory reasons after the final canonical
score is known. Genuine execution and exhaustion protections stay blocking:
score below threshold, sideways protection, ORB over-extension, late two-
candle exhaustion, session counter-trend, market hours, cooldowns, sizing,
contract/LTP/ATR and broker/order safety.
"""

from __future__ import annotations

from typing import Any


VERSION = "OKAI-QUALIFIED-ENTRY-RELEASE-V1"

ADVISORY_EXACT = {
    "VWAP_DIRECTION_REQUIRED",
    "SUPERTREND_DIRECTION_REQUIRED",
    "EMA_TREND_REQUIRED",
    "REVERSAL_CANDLE_AT_ENTRY",
    "ORB_OR_MOMENTUM_TRIGGER_REQUIRED",
    "EMA_EXTENSION_OVER_0.95_ATR",
    "VWAP_EXTENSION_OVER_2.20_ATR",
    "EMA_ANTI_CHASE",
    "VWAP_ANTI_CHASE",
    "ANTI_CHASE_BLOCKED",
    "CHASE_GUARD_BLOCKED",
    "MTF_NOT_CONFIRMED",
}

ADVISORY_PREFIXES = (
    "CORE_CONFIRMATIONS_",
    "REAL_5M_MTF_NOT_CONFIRMED",
    "REAL_5M_MTF_UNAVAILABLE",
    "FRESH_ENTRY_BLOCK:REVERSAL_CANDLE_AT_ENTRY",
    "ENTRY_TIMING_BLOCK:EMA_EXTENSION_OVER_0.95_ATR",
    "ENTRY_TIMING_BLOCK:VWAP_EXTENSION_OVER_2.20_ATR",
)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _candidate(signal: dict[str, Any]) -> str:
    value = str(
        signal.get("candidate_signal")
        or signal.get("signal")
        or "WAIT"
    ).upper()
    return value if value in {"CE", "PE"} else "WAIT"


def _advisory_reason(reason: Any, candidate: str, score: int, minimum: int) -> bool:
    text = str(reason or "").strip()
    upper = text.upper()
    if not upper:
        return False
    if upper in ADVISORY_EXACT or upper.startswith(ADVISORY_PREFIXES):
        return True
    if upper.startswith("SCORE_BELOW_") and score >= minimum:
        return True
    if upper == "NO_DIRECTIONAL_SIGNAL" and candidate in {"CE", "PE"}:
        return True
    return False


def _clean_reasons(
    values: Any,
    candidate: str,
    score: int,
    minimum: int,
) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    removed: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        target = removed if _advisory_reason(text, candidate, score, minimum) else kept
        if text not in target:
            target.append(text)
    return kept, removed


def _repair_signal(signal: Any) -> Any:
    if not isinstance(signal, dict):
        return signal

    output = dict(signal)
    candidate = _candidate(output)
    score = _i(output.get("score"), 0)
    minimum = _i(output.get("min_score"), 82)

    safety, removed_safety = _clean_reasons(
        output.get("safety_gate_reasons"),
        candidate,
        score,
        minimum,
    )
    fresh, removed_fresh = _clean_reasons(
        output.get("fresh_entry_block_reasons"),
        candidate,
        score,
        minimum,
    )
    removed = list(dict.fromkeys(removed_safety + removed_fresh))

    # Do not manufacture permission for an unexplained WAIT. A blocked setup is
    # released only when this patch actually removed a known duplicate veto.
    release_requested = bool(removed)
    protected_flag = bool(
        output.get("sideways_blocked", False)
        or output.get("session_counter_trend_blocked", False)
    )
    qualified = bool(
        candidate in {"CE", "PE"}
        and score >= minimum
        and not safety
        and not fresh
        and not protected_flag
    )
    allowed = bool(
        qualified
        and (
            release_requested
            or output.get("trade_allowed", False)
        )
    )

    warnings = list(output.get("warnings") or [])
    if removed:
        warnings.append(
            "QUALIFIED_ENTRY_DUPLICATE_BLOCKS_OBSERVATION_ONLY:"
            + ",".join(removed)
        )

    output.update({
        "signal": candidate if allowed else "WAIT",
        "candidate_signal": candidate,
        "trade_allowed": allowed,
        "strategy_qualified": allowed,
        "safety_gate_passed": allowed,
        "safety_gate_reasons": safety,
        "fresh_entry_ok": not fresh,
        "fresh_entry_block_reasons": fresh,
        "mandatory_confirmations_blocking": False,
        "reversal_candle_blocking": False,
        "mtf_confirmation_blocking": False,
        "qualified_entry_release_applied": bool(removed),
        "qualified_entry_release_reasons": removed,
        "qualified_entry_release_version": VERSION,
        "warnings": list(dict.fromkeys(warnings)),
    })
    return output


def _repair_scan(scan: Any) -> Any:
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan

    signal = _repair_signal(scan.get("signal_data") or {})
    if not isinstance(signal, dict):
        return scan

    market = dict(scan.get("market_data") or {})
    window_open = signal.get("entry_window_open", True) is not False
    execution_allowed = bool(signal.get("trade_allowed", False) and window_open)
    signal["execution_allowed"] = execution_allowed
    if execution_allowed:
        signal["execution_block_reason"] = ""

    market.update({
        "signal": signal.get("signal", "WAIT"),
        "signal_score": _i(signal.get("score"), 0),
        "execution_allowed": execution_allowed,
    })
    if execution_allowed:
        market["execution_block_reason"] = ""

    payload = signal.get("live_score_breakdown")
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.update({
            "trade_allowed": bool(signal.get("trade_allowed", False)),
            "execution_allowed": execution_allowed,
            "execution_block_reason": signal.get("execution_block_reason", ""),
            "safety_gate_reasons": list(signal.get("safety_gate_reasons") or []),
            "qualified_entry_release_applied": bool(
                signal.get("qualified_entry_release_applied", False)
            ),
        })
        signal["live_score_breakdown"] = payload

    scan["signal_data"] = signal
    scan["market_data"] = market
    scan["score"] = _i(signal.get("score"), 0)
    scan["decision_score"] = scan["score"]
    scan["execution_allowed"] = execution_allowed
    if execution_allowed:
        scan["execution_block_reason"] = ""
    return scan


def apply_qualified_entry_release_patch() -> bool:
    from bot import angel_fetcher
    from bot import auto_portfolio_runtime as runtime
    from bot import routes
    from bot import strategy

    if getattr(runtime, "_okai_qualified_entry_release_v1", False):
        return True

    original_signal = strategy.get_full_signal

    def qualified_signal(*args, **kwargs):
        return _repair_signal(original_signal(*args, **kwargs))

    strategy.get_full_signal = qualified_signal
    routes.get_full_signal = qualified_signal
    angel_fetcher.get_full_signal = qualified_signal

    original_build_scan = runtime._build_scan

    def build_scan_with_release(*args, **kwargs):
        return _repair_scan(original_build_scan(*args, **kwargs))

    runtime._build_scan = build_scan_with_release

    try:
        from bot import live_scan_history_fallback_patch as replay

        original_replay_scan = replay._replay_scan

        def replay_scan_with_release(*args, **kwargs):
            return _repair_scan(original_replay_scan(*args, **kwargs))

        replay._replay_scan = replay_scan_with_release
        replay._okai_qualified_entry_release_v1 = True
    except Exception:
        pass

    strategy._okai_qualified_entry_release_v1 = True
    runtime._okai_qualified_entry_release_v1 = True
    return True


__all__ = [
    "VERSION",
    "_repair_scan",
    "_repair_signal",
    "apply_qualified_entry_release_patch",
]
