"""Targeted release for the legacy FINAL VWAP ST EMA MTF MISALIGNED veto.

This patch removes only that exact composite veto after all strategy wrappers have
run. It does not weaken score, sideways, exhaustion, session, timing, sizing,
contract, broker, order, ATR, cooldown, or other safety reasons.
"""

from __future__ import annotations

from typing import Any

from bot import auto_portfolio_runtime as runtime

VERSION = "FINAL_MTF_MISALIGNED_RELEASE_V1"
TARGETS = {
    "FINAL VWAP ST EMA MTF MISALIGNED",
    "FINAL_VWAP_ST_EMA_MTF_MISALIGNED",
}


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().replace("_", " ").split())


def _is_target(value: Any) -> bool:
    normalized = _norm(value)
    return normalized == "FINAL VWAP ST EMA MTF MISALIGNED"


def _clean(values: Any) -> tuple[list[str], bool]:
    kept: list[str] = []
    removed = False
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        if _is_target(text):
            removed = True
            continue
        if text not in kept:
            kept.append(text)
    return kept, removed


def _repair_scan(scan: Any) -> Any:
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan

    signal = dict(scan.get("signal_data") or {})
    if not signal:
        return scan

    removed = False
    for key in (
        "safety_gate_reasons",
        "fresh_entry_block_reasons",
        "entry_timing_block_reasons",
    ):
        cleaned, did_remove = _clean(signal.get(key))
        signal[key] = cleaned
        removed = removed or did_remove

    for key in ("execution_block_reason", "entry_block_reason"):
        if _is_target(signal.get(key)):
            signal[key] = ""
            removed = True

    if _is_target(scan.get("execution_block_reason")):
        scan["execution_block_reason"] = ""
        removed = True
    if _is_target(scan.get("entry_block_reason")):
        scan["entry_block_reason"] = ""
        removed = True

    if not removed:
        return scan

    candidate = str(signal.get("candidate_signal") or signal.get("signal") or "WAIT").upper()
    try:
        score = int(round(float(signal.get("score") or 0)))
    except Exception:
        score = 0
    try:
        minimum = int(round(float(signal.get("min_score") or 82)))
    except Exception:
        minimum = 82

    remaining = []
    for key in (
        "safety_gate_reasons",
        "fresh_entry_block_reasons",
        "entry_timing_block_reasons",
    ):
        remaining.extend(signal.get(key) or [])

    protected_flag = bool(
        signal.get("sideways_blocked", False)
        or signal.get("session_counter_trend_blocked", False)
        or signal.get("entry_window_open", True) is False
        or signal.get("late_two_candle_exhaustion", False)
        or signal.get("late_two_candle_exhaustion_blocked", False)
        or signal.get("choppy_market_blocked", False)
        or signal.get("market_regime_blocked", False)
    )

    eligible = bool(
        candidate in {"CE", "PE"}
        and score >= minimum
        and not remaining
        and not protected_flag
    )

    signal["final_mtf_misaligned_blocking"] = False
    signal["final_mtf_misaligned_release_applied"] = True
    signal["final_mtf_misaligned_release_version"] = VERSION

    if eligible:
        signal["signal"] = candidate
        signal["trade_allowed"] = True
        signal["strategy_qualified"] = True
        signal["safety_gate_passed"] = True
        signal["fresh_entry_ok"] = True
        signal["execution_allowed"] = True
        signal["execution_block_reason"] = ""
        signal["entry_block_reason"] = ""
        scan["execution_allowed"] = True
        scan["execution_block_reason"] = ""
        scan["entry_block_reason"] = ""
        market = dict(scan.get("market_data") or {})
        market["signal"] = candidate
        market["execution_allowed"] = True
        market["execution_block_reason"] = ""
        scan["market_data"] = market

    scan["signal_data"] = signal
    return scan


def apply_final_mtf_misaligned_release_patch() -> None:
    if getattr(runtime, "_okai_final_mtf_misaligned_release_v1", False):
        return

    original_scan_angel = runtime._scan_angel
    original_scan_multi = runtime._scan_multi

    def scan_angel(*args, **kwargs):
        return [_repair_scan(scan) for scan in original_scan_angel(*args, **kwargs)]

    def scan_multi(*args, **kwargs):
        return [_repair_scan(scan) for scan in original_scan_multi(*args, **kwargs)]

    runtime._scan_angel = scan_angel
    runtime._scan_multi = scan_multi
    runtime._okai_final_mtf_misaligned_release_v1 = True


__all__ = ["VERSION", "_repair_scan", "apply_final_mtf_misaligned_release_patch"]
