"""Final choppy-range entry guard with a strong-breakout escape hatch."""

from __future__ import annotations

from typing import Any

from bot import auto_portfolio_runtime as runtime
from bot.regime_accuracy_confirmation_patch import (
    CHOPPY_BLOCK_REASON,
    _choppy_assessment,
)


VERSION = "OKAI-CHOPPY-MARKET-GUARD-V2-REPLAY-ENFORCED"


def _candidate(signal: dict[str, Any]) -> str:
    value = str(
        signal.get("candidate_signal")
        or signal.get("signal")
        or "WAIT"
    ).upper()
    return value if value in {"CE", "PE"} else "WAIT"


def _apply_guard(scan: Any, frame: Any) -> Any:
    if not isinstance(scan, dict) or scan.get("status") != "OK":
        return scan
    signal = scan.get("signal_data")
    market = scan.get("market_data")
    if not isinstance(signal, dict) or not isinstance(market, dict):
        return scan

    side = _candidate(signal)
    real_mtf = dict(
        signal.get("real_mtf_5m")
        or scan.get("real_mtf_5m")
        or {}
    )
    assessment_market = dict(market)
    assessment_market["mtf_confirmed"] = bool(
        real_mtf.get("available", False)
        and str(real_mtf.get("side") or "WAIT").upper() == side
    )
    assessment = _choppy_assessment(
        frame,
        assessment_market,
        side,
        int(round(float(signal.get("score") or 0))),
        int(round(float(signal.get("min_score") or 82))),
    )

    signal["choppy_market_guard"] = dict(assessment)
    signal["choppy_market_guard_version"] = VERSION
    market["choppy_market_guard"] = dict(assessment)
    scan["choppy_market_guard"] = dict(assessment)

    if assessment.get("hard_block") and side in {"CE", "PE"}:
        signal["signal"] = "WAIT"
        signal["trade_allowed"] = False
        signal["strategy_qualified"] = False
        signal["safety_gate_passed"] = False
        signal["sideways_blocked"] = True
        signal["choppy_market_blocked"] = True
        safety = list(signal.get("safety_gate_reasons") or [])
        if CHOPPY_BLOCK_REASON not in safety:
            safety.append(CHOPPY_BLOCK_REASON)
        signal["safety_gate_reasons"] = safety
        signal.setdefault("warnings", []).append(
            "Choppy range: strong 5-minute-confirmed breakout ka wait"
        )
        market["signal"] = "WAIT"
        market["execution_allowed"] = False
        scan["execution_allowed"] = False
        scan["execution_block_reason"] = CHOPPY_BLOCK_REASON
    else:
        signal["choppy_market_blocked"] = False
        if assessment.get("strong_breakout_override"):
            signal.setdefault("warnings", []).append(
                "CHOPPY_GUARD_STRONG_BREAKOUT_ALLOWED"
            )
    return scan


def apply_choppy_market_guard_patch() -> bool:
    if getattr(runtime, "_okai_choppy_market_guard_v2", False):
        return True
    previous_build_scan = runtime._build_scan

    def build_scan_with_choppy_guard(user_id, underlying, frame, profile, loss_streak):
        return _apply_guard(
            previous_build_scan(user_id, underlying, frame, profile, loss_streak),
            frame,
        )

    runtime._build_scan = build_scan_with_choppy_guard
    # The production AUTO engine uses replay-first scans.  V1 wrapped only
    # ``runtime._build_scan``, so the live replay path could bypass the choppy
    # guard even though its unit tests passed.  Install the same final guard at
    # the actual replay boundary.
    try:
        from bot import live_scan_history_fallback_patch as replay

        previous_replay_scan = replay._replay_scan

        def replay_scan_with_choppy_guard(
            user_id,
            underlying,
            frame,
            profile,
            source,
            notes,
        ):
            return _apply_guard(
                previous_replay_scan(
                    user_id,
                    underlying,
                    frame,
                    profile,
                    source,
                    notes,
                ),
                frame,
            )

        replay._replay_scan = replay_scan_with_choppy_guard
        replay._okai_choppy_market_guard_v2 = True
    except Exception:
        # Direct scans remain guarded and production diagnostics stay visible
        # if the optional replay module is not present in a local build.
        pass
    runtime._okai_choppy_market_guard_v1 = True
    runtime._okai_choppy_market_guard_v2 = True
    return True


__all__ = ["VERSION", "_apply_guard", "apply_choppy_market_guard_patch"]
