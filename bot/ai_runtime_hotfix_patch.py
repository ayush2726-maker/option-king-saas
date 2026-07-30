"""AI runtime hotfix bindings for live monitor display.

Monitoring/display only. These helpers do not change entries, exits, quantity,
SL, risk rules, trade blocking, or order execution.
"""
from __future__ import annotations

from typing import Any, Dict


_LAST_GOOD_PROBE: Dict[int, Dict[str, Any]] = {}


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except (TypeError, ValueError):
        return float(default)


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
            advanced._okai_angel_pcr_force_bound_v2 = True
    except Exception:
        # Fail closed: startup must never break because this is display-only.
        pass


def apply_option_payload_pcr_injection_patch() -> None:
    """Recover PCR on the final Advanced-AI option payload, including cached rows.

    advanced_intelligence_v2 caches broker option payloads for 60 seconds. If an
    older cached payload was built before the PCR wrapper was rebound, the live
    probe can still show PCR as missing. This wrapper runs recovery/diagnostics on
    the final payload returned to the monitor, so the mobile card receives either
    PCR or a visible PCR error/source.
    """
    try:
        from bot.angel_pcr_recovery_patch import (
            apply_angel_pcr_recovery_patch,
            recover_pcr_for_result,
        )
        from bot import advanced_intelligence_v2 as advanced

        if getattr(advanced, "_okai_option_payload_pcr_injection_v1", False):
            return

        try:
            apply_angel_pcr_recovery_patch()
        except Exception:
            pass

        original_option_payload = advanced._option_payload

        def option_payload_with_pcr(user_id, market):
            result = dict(original_option_payload(user_id, market) or {})
            return recover_pcr_for_result(user_id, market or {}, result)

        advanced._option_payload = option_payload_with_pcr
        advanced._okai_option_payload_pcr_injection_v1 = True
    except Exception:
        pass


def _pcr_diagnostics(option: Dict[str, Any]) -> Dict[str, Any]:
    """Attach explicit PCR source/error fields so mobile never silently shows --."""
    result = dict(option or {})
    if _f(result.get("pcr"), 0.0) > 0:
        result.setdefault("pcr_source", "CHAIN_OI")
        return result

    result.setdefault("pcr_source", "UNAVAILABLE")
    call_oi = _f(result.get("total_call_oi"), 0.0)
    put_oi = _f(result.get("total_put_oi"), 0.0)
    if not result.get("pcr_error"):
        if call_oi <= 0 and put_oi <= 0:
            result["pcr_error"] = "ANGEL_OI_MISSING_FOR_CE_PE_AND_NATIVE_PCR_EMPTY"
        elif call_oi <= 0:
            result["pcr_error"] = f"CALL_OI_ZERO put_oi={round(put_oi, 2)}"
        else:
            result["pcr_error"] = "PCR_VALUE_NOT_BUILT_DESPITE_OI"

    reasons = list(result.get("reasons") or [])
    if "PCR_DIAGNOSTIC_VISIBLE" not in reasons:
        reasons.insert(0, "PCR_DIAGNOSTIC_VISIBLE")
    result["reasons"] = list(dict.fromkeys(reasons))[:15]
    return result


def _probe_is_good(probe: Dict[str, Any]) -> bool:
    option = dict(probe.get("option_intelligence") or {})
    return bool(
        probe.get("success")
        and _i(option.get("data_coverage_score")) > 0
        and option
    )


def _probe_is_timeout_or_blank(probe: Dict[str, Any]) -> bool:
    option = dict(probe.get("option_intelligence") or {})
    reason = str(probe.get("reason") or option.get("pcr_error") or "")
    return bool(
        _i(option.get("data_coverage_score")) <= 0
        or "CONNECTTIMEOUT" in reason.upper()
        or "MAX RETRIES" in reason.upper()
        or "GETLTPDATA" in reason.upper()
    )


def _live_probe_row(probe: Dict[str, Any]) -> Dict[str, Any]:
    option = _pcr_diagnostics(dict(probe.get("option_intelligence") or {}))
    reasons = ["LIVE_PROBE_CURRENT_MARKET"]
    if option.get("pcr_source"):
        reasons.append("PCR_SOURCE_" + str(option.get("pcr_source")))
    if option.get("pcr_error"):
        reasons.append("PCR_ERROR_" + str(option.get("pcr_error"))[:90])
    if probe.get("reason"):
        reasons.append(str(probe.get("reason")))
    reasons.extend(list(option.get("reasons") or []))

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
        "reasons": list(dict.fromkeys(reasons))[:15],
        "display_only": True,
        "live_probe": True,
        "trade_blocking": False,
        "order_execution": False,
    }


def apply_live_probe_recent_first_patch() -> None:
    """Put the live probe first so mobile does not show a stale DB decision row.

    If Angel times out for one cycle, keep the last good option snapshot for
    display instead of overwriting the card with 0% coverage. The stale flag is
    display-only and never trains the model or affects trading.
    """
    try:
        from bot import advanced_intelligence_v2 as advanced

        if getattr(advanced, "_okai_live_probe_recent_first_v2", False):
            return

        original_summary = advanced.get_advanced_summary

        def get_advanced_summary(user_id, recent_limit=20):
            data = dict(original_summary(user_id, recent_limit=recent_limit) or {})
            probe = dict(data.get("current_probe") or {})
            if not probe:
                return data

            uid = int(user_id)
            if _probe_is_good(probe):
                _LAST_GOOD_PROBE[uid] = dict(probe)
                display_probe = probe
                data["using_stale_ai_probe_for_display"] = False
            elif _probe_is_timeout_or_blank(probe) and _LAST_GOOD_PROBE.get(uid):
                display_probe = dict(_LAST_GOOD_PROBE[uid])
                option = dict(display_probe.get("option_intelligence") or {})
                reasons = list(option.get("reasons") or [])
                reasons.insert(0, "ANGEL_TIMEOUT_SHOWING_LAST_GOOD_OPTION_DATA")
                option["reasons"] = list(dict.fromkeys(reasons))[:15]
                option["stale_due_to_angel_timeout"] = True
                option["latest_error"] = str(probe.get("reason") or "")[:260]
                display_probe["option_intelligence"] = option
                display_probe["reason"] = "ANGEL_TIMEOUT_SHOWING_LAST_GOOD_OPTION_DATA"
                data["using_stale_ai_probe_for_display"] = True
                data["latest_probe_error"] = str(probe.get("reason") or "")[:260]
            else:
                display_probe = probe
                data["using_stale_ai_probe_for_display"] = False

            live_row = _live_probe_row(display_probe)
            recent = [
                dict(item)
                for item in list(data.get("recent_decisions") or [])
                if dict(item).get("id") != "LIVE_PROBE_DISPLAY_ONLY"
            ]
            limit = max(1, min(_i(recent_limit, 20), 50))
            data["recent_decisions"] = [live_row] + recent[: max(0, limit - 1)]
            data["using_live_probe_for_display"] = True
            data["current_probe"] = display_probe
            if display_probe.get("broker"):
                data["active_broker"] = display_probe.get("broker")
            return data

        advanced.get_advanced_summary = get_advanced_summary
        advanced._okai_live_probe_recent_first_v1 = True
        advanced._okai_live_probe_recent_first_v2 = True
    except Exception:
        pass
