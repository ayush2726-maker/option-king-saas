"""AI runtime hotfix bindings for live monitor display.

Monitoring/display only. These helpers do not change entries, exits, quantity,
SL, risk rules, trade blocking, or order execution.
"""
from __future__ import annotations

from typing import Any, Dict


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def apply_angel_pcr_force_binding() -> None:
    """Ensure Advanced AI uses the Angel PCR-recovered broker function.

    bot.__init__ may apply the PCR patch before advanced_intelligence_v2 is fully
    imported. In that case broker_intelligence is patched, but the already-bound
    name inside advanced_intelligence_v2 can still point at the old function. This
    safely rebinds it after all AI modules are loaded.
    """
    try:
        from bot.angel_pcr_recovery_patch import apply_angel_pcr_recovery_patch
        from bot import broker_intelligence as broker
        from bot import advanced_intelligence_v2 as advanced

        try:
            apply_angel_pcr_recovery_patch()
        except Exception:
            pass

        if callable(getattr(broker, "get_broker_intelligence", None)):
            advanced.get_broker_intelligence = broker.get_broker_intelligence
            advanced._okai_angel_pcr_force_bound_v1 = True
    except Exception:
        # Fail closed: startup must never break because this is display-only.
        pass


def _live_probe_row(probe: Dict[str, Any]) -> Dict[str, Any]:
    option = dict(probe.get("option_intelligence") or {})
    reasons = []
    if probe.get("reason"):
        reasons.append(str(probe.get("reason")))
    reasons.extend(list(option.get("reasons") or []))
    reasons.append("LIVE_PROBE_CURRENT_MARKET")
    if option.get("pcr_source"):
        reasons.append("PCR_SOURCE_" + str(option.get("pcr_source")))
    if option.get("pcr_error"):
        reasons.append("PCR_ERROR_" + str(option.get("pcr_error"))[:90])

    return {
        "id": "LIVE_PROBE_DISPLAY_ONLY",
        "broker": probe.get("broker"),
        "symbol": probe.get("underlying"),
        "spot": probe.get("spot"),
        "advanced_decision": "COLLECTING",
        "advanced_confidence": 0,
        "advanced_probabilities": {},
        "option_decision": option.get("option_direction") or "NO_TRADE",
        "option_confidence": _i(option.get("option_confidence")),
        "data_coverage_score": _i(option.get("data_coverage_score")),
        "option_risk_score": _i(option.get("risk_score")),
        "option_summary": option,
        "reasons": list(dict.fromkeys(reasons))[:12],
        "display_only": True,
        "live_probe": True,
        "trade_blocking": False,
        "order_execution": False,
    }


def apply_live_probe_recent_first_patch() -> None:
    """Put the live probe first so mobile does not show a stale DB decision row."""
    try:
        from bot import advanced_intelligence_v2 as advanced

        if getattr(advanced, "_okai_live_probe_recent_first_v1", False):
            return

        original_summary = advanced.get_advanced_summary

        def get_advanced_summary(user_id, recent_limit=20):
            data = dict(original_summary(user_id, recent_limit=recent_limit) or {})
            probe = dict(data.get("current_probe") or {})
            if not probe:
                return data

            live_row = _live_probe_row(probe)
            recent = [
                dict(item)
                for item in list(data.get("recent_decisions") or [])
                if dict(item).get("id") != "LIVE_PROBE_DISPLAY_ONLY"
            ]
            limit = max(1, min(_i(recent_limit, 20), 50))
            data["recent_decisions"] = [live_row] + recent[: max(0, limit - 1)]
            data["using_live_probe_for_display"] = True
            if probe.get("broker"):
                data["active_broker"] = probe.get("broker")
            return data

        advanced.get_advanced_summary = get_advanced_summary
        advanced._okai_live_probe_recent_first_v1 = True
    except Exception:
        pass
