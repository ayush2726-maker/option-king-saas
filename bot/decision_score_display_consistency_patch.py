"""Keep the score shown to users identical to the score used for entry.

The live score breakdown intentionally calculates a proportional visual-strength
number for each row.  That number is useful for diagnostics, but it must never
replace the binary/decision score used by the trading engine.  Otherwise the app
can show an impossible state such as ``91/82`` together with ``SAFETY_BLOCKED``.

This patch runs after the existing active-strategy and breakdown wrappers. Every
public score field stays on the actual entry decision score. The proportional
strength number remains available only as
``diagnostic_visual_strength_score``.

Trading logic, thresholds, order placement, quantity, SL, exits and cooldowns
are unchanged.
"""

from __future__ import annotations

from typing import Any

from bot import auto_portfolio_runtime as runtime


SCORE_MODE = "CANONICAL_DECISION_SCORE_PUBLIC_V2"


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
            "diagnostic_visual_strength_score",
            payload.get(
                "display_score",
                payload.get(
                    "visual_strength_score",
                    payload.get("component_total", payload.get("score", decision)),
                ),
            ),
        ),
        decision,
    )

    fixed_payload = dict(payload)
    fixed_payload.update(
        {
            "score": decision,
            "decision_score": decision,
            "display_score": decision,
            "visual_strength_score": decision,
            "diagnostic_visual_strength_score": visual,
            "score_mode": SCORE_MODE,
        }
    )

    signal["score"] = decision
    signal["decision_score"] = decision
    signal["display_score"] = decision
    signal["visual_strength_score"] = decision
    signal["diagnostic_visual_strength_score"] = visual
    signal["live_score_breakdown"] = fixed_payload
    signal["score_mode"] = SCORE_MODE

    scan["score"] = decision
    scan["decision_score"] = decision
    scan["display_score"] = decision
    scan["visual_strength_score"] = decision
    scan["diagnostic_visual_strength_score"] = visual
    scan["live_score_breakdown"] = fixed_payload
    scan["score_mode"] = SCORE_MODE
    return scan


def _install_final_entry_guards() -> None:
    # Run in this order: correct the score first, then attach the final opening
    # and loss circuit as the outermost entry/close protection.
    try:
        from bot.real_mtf_session_guard_patch import (
            apply_real_mtf_session_guard_patch,
        )

        apply_real_mtf_session_guard_patch()
    except Exception:
        pass

    try:
        from bot.opening_orb_loss_circuit_patch import (
            apply_opening_orb_loss_circuit_patch,
        )

        apply_opening_orb_loss_circuit_patch()
    except Exception:
        pass


def apply_decision_score_display_consistency_patch() -> None:
    if getattr(runtime, "_okai_decision_score_display_consistency_v1", False):
        _install_final_entry_guards()
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
            payload.get(
                "decision_score",
                signal.get("score", data.get("decision_score", data.get("score", 0))),
            ),
            _i(data.get("score"), 0),
        )
        visual = _i(
            payload.get(
                "diagnostic_visual_strength_score",
                payload.get("display_score", data.get("display_score", decision)),
            ),
            decision,
        )

        data.update(
            {
                "score": decision,
                "decision_score": decision,
                "display_score": decision,
                "visual_strength_score": decision,
                "diagnostic_visual_strength_score": visual,
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
        payload = (
            signal.get("live_score_breakdown")
            or display.get("live_score_breakdown")
            or {}
        )
        decision = _i(
            payload.get(
                "decision_score",
                signal.get(
                    "score",
                    state.get("decision_score", state.get("score", 0)),
                ),
            ),
            _i(state.get("score"), 0),
        )
        visual = _i(
            payload.get(
                "diagnostic_visual_strength_score",
                payload.get("display_score", state.get("display_score", decision)),
            ),
            decision,
        )

        state["score"] = decision
        state["decision_score"] = decision
        state["display_score"] = decision
        state["visual_strength_score"] = decision
        state["diagnostic_visual_strength_score"] = visual
        state["score_mode"] = SCORE_MODE

    runtime._build_scan = build_scan_with_decision_score
    runtime._summary = summary_with_decision_score
    runtime._state_update = state_update_with_decision_score
    runtime._okai_decision_score_display_consistency_v1 = True

    _install_final_entry_guards()
