"""Keep the score shown to users identical to the score used for entry.

The live score breakdown intentionally calculates a proportional visual-strength
number for each row.  That number is useful for diagnostics, but it must never
replace the binary/decision score used by the trading engine.  Otherwise the app
can show an impossible state such as ``91/82`` together with ``SAFETY_BLOCKED``.

This patch runs after the existing active-strategy and breakdown wrappers.  It
keeps:
- ``score`` / ``decision_score`` = the actual entry decision score;
- ``display_score`` / ``visual_strength_score`` = proportional visual strength.

Trading logic, thresholds, order placement, quantity, SL, exits and cooldowns
are unchanged.
"""

from __future__ import annotations

from typing import Any

from bot import auto_portfolio_runtime as runtime


SCORE_MODE = "DECISION_SCORE_PRIMARY_VISUAL_STRENGTH_SECONDARY"


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _normalise_scan(scan: Any) -> Any:
    if not isinstance(scan, dict):
        return scan

    signal = scan.get("signal_data")
    if not isinstance(signal, dict):
        return scan

    payload = signal.get("live_score_breakdown")
    if not isinstance(payload, dict):
        payload = scan.get("live_score_breakdown")
    if not isinstance(payload, dict):
        return scan

    decision = _i(
        payload.get(
            "decision_score",
            signal.get("score", scan.get("decision_score", scan.get("score", 0))),
        ),
        _i(signal.get("score"), 0),
    )
    visual = _i(
        payload.get(
            "display_score",
            payload.get(
                "visual_strength_score",
                payload.get("component_total", payload.get("score", decision)),
            ),
        ),
        decision,
    )

    fixed_payload = dict(payload)
    fixed_payload.update(
        {
            "score": decision,
            "decision_score": decision,
            "display_score": visual,
            "visual_strength_score": visual,
            "score_mode": SCORE_MODE,
        }
    )

    signal["score"] = decision
    signal["decision_score"] = decision
    signal["display_score"] = visual
    signal["visual_strength_score"] = visual
    signal["live_score_breakdown"] = fixed_payload
    signal["score_mode"] = SCORE_MODE

    scan["score"] = decision
    scan["decision_score"] = decision
    scan["display_score"] = visual
    scan["visual_strength_score"] = visual
    scan["live_score_breakdown"] = fixed_payload
    scan["score_mode"] = SCORE_MODE
    return scan


def apply_decision_score_display_consistency_patch() -> None:
    if getattr(runtime, "_okai_decision_score_display_consistency_v1", False):
        try:
            from bot.real_mtf_session_guard_patch import (
                apply_real_mtf_session_guard_patch,
            )

            apply_real_mtf_session_guard_patch()
        except Exception:
            pass
        return

    original_build_scan = runtime._build_scan
    original_summary = runtime._summary
    original_state_update = runtime._state_update

    def build_scan_with_decision_score(*args, **kwargs):
        return _normalise_scan(original_build_scan(*args, **kwargs))

    def summary_with_decision_score(scan):
        scan = _normalise_scan(scan)
        data = original_summary(scan)
        if not isinstance(data, dict) or not isinstance(scan, dict):
            return data

        signal = scan.get("signal_data") or {}
        payload = signal.get("live_score_breakdown") or scan.get("live_score_breakdown") or {}
        decision = _i(
            payload.get("decision_score", signal.get("score", data.get("decision_score", data.get("score", 0)))),
            _i(data.get("score"), 0),
        )
        visual = _i(
            payload.get("display_score", data.get("display_score", decision)),
            decision,
        )

        data.update(
            {
                "score": decision,
                "decision_score": decision,
                "display_score": visual,
                "visual_strength_score": visual,
                "score_mode": SCORE_MODE,
            }
        )
        return data

    def state_update_with_decision_score(state, scans, selected, settings, rows):
        fixed_scans = [_normalise_scan(scan) for scan in (scans or [])]
        fixed_selected = _normalise_scan(selected) if selected is not None else None
        original_state_update(state, fixed_scans, fixed_selected, settings, rows)

        display = runtime._display_scan(fixed_scans, fixed_selected, settings)
        if not isinstance(display, dict):
            return

        signal = display.get("signal_data") or {}
        payload = signal.get("live_score_breakdown") or display.get("live_score_breakdown") or {}
        decision = _i(
            payload.get("decision_score", signal.get("score", state.get("decision_score", state.get("score", 0)))),
            _i(state.get("score"), 0),
        )
        visual = _i(
            payload.get("display_score", state.get("display_score", decision)),
            decision,
        )

        state["score"] = decision
        state["decision_score"] = decision
        state["display_score"] = visual
        state["visual_strength_score"] = visual
        state["score_mode"] = SCORE_MODE

    runtime._build_scan = build_scan_with_decision_score
    runtime._summary = summary_with_decision_score
    runtime._state_update = state_update_with_decision_score
    runtime._okai_decision_score_display_consistency_v1 = True

    # Install the final safety correction only after every existing score and
    # display wrapper has been attached.  This guarantees the real completed
    # 5-minute confirmation is the score used by both entry and UI.
    try:
        from bot.real_mtf_session_guard_patch import (
            apply_real_mtf_session_guard_patch,
        )

        apply_real_mtf_session_guard_patch()
    except Exception:
        pass
