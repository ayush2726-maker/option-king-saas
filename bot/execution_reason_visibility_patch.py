"""Expose the exact AUTO execution result on the existing live-score warning feed.

The mobile Final Decision card already renders scan warnings. Runtime entry
attempts, however, were kept only on the engine state, so the card fell back to
a generic EXECUTION BLOCK message even when the strategy itself had qualified.
This patch mirrors the latest concrete execution result into the selected scan
warnings and live-score payload without changing any entry/risk decision.
"""

from __future__ import annotations

from bot import auto_portfolio_runtime as runtime


PATCH_VERSION = "EXECUTION_REASON_VISIBILITY_V1"


def _text(value):
    return str(value or "").strip()


def _details_text(details):
    if not isinstance(details, dict) or not details:
        return ""
    parts = []
    for key in (
        "message", "symbol", "token", "exchange", "ltp", "one_lot_cost",
        "available_after_reserve", "open_positions", "remaining_seconds",
        "status", "order_id",
    ):
        value = details.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{key}={value}")
    return " | ".join(parts[:5])


def _latest_execution(state):
    attempts = state.get("entry_candidate_attempts") or []
    if isinstance(attempts, list) and attempts:
        latest = attempts[-1]
        if isinstance(latest, dict) and not latest.get("opened"):
            return {
                "reason": _text(latest.get("reason")) or "ENTRY_NOT_OPENED",
                "stage": _text(latest.get("stage")) or "ENTRY_ATTEMPT",
                "underlying": _text(latest.get("underlying")),
                "side": _text(latest.get("side")),
                "details": latest.get("details") or {},
            }

    attempt = state.get("last_entry_attempt") or state.get("entry_attempt") or state.get("entry_guard")
    if isinstance(attempt, dict) and attempt.get("allowed") is False:
        return {
            "reason": _text(attempt.get("reason")) or "ENTRY_NOT_OPENED",
            "stage": _text(attempt.get("stage")) or "ENTRY_GUARD",
            "underlying": _text(attempt.get("underlying")),
            "side": _text(attempt.get("side")),
            "details": attempt.get("details") or {},
        }

    permission = state.get("entry_permission")
    if isinstance(permission, dict) and permission.get("allowed") is False:
        return {
            "reason": _text(permission.get("reason")) or "ENTRY_PERMISSION_BLOCKED",
            "stage": "ENTRY_PERMISSION",
            "underlying": "",
            "side": "",
            "details": permission,
        }
    return None


def _append_warning(scan, message):
    if not isinstance(scan, dict):
        return
    signal = scan.get("signal_data")
    if not isinstance(signal, dict):
        return

    warnings = list(signal.get("warnings") or [])
    warnings = [w for w in warnings if not _text(w).startswith("EXECUTION_BLOCK_EXACT:")]
    warnings.append(message)
    signal["warnings"] = warnings[-12:]

    payload = signal.get("live_score_breakdown")
    if isinstance(payload, dict):
        pw = list(payload.get("warnings") or [])
        pw = [w for w in pw if not _text(w).startswith("EXECUTION_BLOCK_EXACT:")]
        pw.append(message)
        payload["warnings"] = pw[-12:]
        signal["live_score_breakdown"] = payload
        scan["live_score_breakdown"] = payload


def apply_execution_reason_visibility_patch():
    if getattr(runtime, "_okai_execution_reason_visibility_v1", False):
        return

    original_state_update = runtime._state_update

    def state_update_with_exact_reason(state, scans, selected, settings, rows):
        original_state_update(state, scans, selected, settings, rows)
        execution = _latest_execution(state)
        if not execution:
            state.pop("execution_block_exact", None)
            return

        reason = execution["reason"]
        stage = execution["stage"]
        detail = _details_text(execution.get("details"))
        message = f"EXECUTION_BLOCK_EXACT: {reason} | stage={stage}"
        if detail:
            message += " | " + detail

        state["execution_block_exact"] = {
            **execution,
            "message": message,
            "version": PATCH_VERSION,
        }
        state["entry_block_reason"] = reason
        state["last_entry_block_reason"] = reason

        target = selected
        if target is None and isinstance(scans, list):
            candidates = [
                scan for scan in scans
                if isinstance(scan, dict)
                and str(scan.get("status") or "").upper() == "OK"
                and str((scan.get("signal_data") or {}).get("candidate_signal") or (scan.get("signal_data") or {}).get("signal") or "").upper() in {"CE", "PE"}
            ]
            candidates.sort(key=lambda s: int(float((s.get("signal_data") or {}).get("score") or 0)), reverse=True)
            target = candidates[0] if candidates else None
        _append_warning(target, message)

    runtime._state_update = state_update_with_exact_reason
    runtime._okai_execution_reason_visibility_v1 = True
