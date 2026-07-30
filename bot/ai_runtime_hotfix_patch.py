"""AI runtime hotfix bindings for live monitor display.

Monitoring/display only. These helpers do not change entries, exits, quantity,
SL, risk rules, trade blocking, or order execution.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


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


def _selected_broker_name(user_id: int) -> Optional[str]:
    try:
        from broker.selected_broker_control import selected_broker_name

        name = selected_broker_name(int(user_id))
        return str(name or "").lower() or None
    except Exception:
        return None


def _selected_broker_creds(user_id: int) -> Tuple[Optional[str], Optional[dict]]:
    """Read the same selected broker row that the Settings screen uses."""
    conn = None
    try:
        from auth.utils import decrypt_credential
        from broker.selection import get_selected_broker
        from database import get_db

        conn = get_db()
        row = get_selected_broker(conn, int(user_id))
        if row is None:
            return None, None
        broker_name = str(row["broker_name"] or "").lower().strip()
        return broker_name, {
            "client_id": row["client_id"],
            "api_key": decrypt_credential(row["api_key"]),
            "password": decrypt_credential(row["api_secret"]),
            "totp_secret": (
                decrypt_credential(row["totp_secret"])
                if row["totp_secret"]
                else None
            ),
        }
    except Exception:
        return None, None
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _bind_selected_broker_source() -> None:
    """Make Advanced-AI broker intelligence use the selected broker source of truth."""
    try:
        from bot import broker_intelligence as broker
        from bot import market_routes

        market_routes._get_active_broker = _selected_broker_creds
        broker._get_active_broker = _selected_broker_creds
        broker._okai_selected_broker_bound_v1 = True
    except Exception:
        pass


def _clear_advanced_option_cache_if_broker_changed(user_id: int) -> Optional[str]:
    """Drop cached option intelligence when the user switches broker."""
    selected = _selected_broker_name(int(user_id))
    if not selected:
        return None
    try:
        from bot import advanced_intelligence_v2 as advanced

        cached = dict(advanced._option_cache.get(int(user_id)) or {})
        cached_broker = str(cached.get("broker") or "").lower()
        if cached_broker and cached_broker != selected:
            advanced._option_cache.pop(int(user_id), None)
            advanced._option_fetch_mono.pop(int(user_id), None)
            last_good = getattr(advanced, "_okai_last_good_option_payload", None)
            if isinstance(last_good, dict):
                last_good.pop(int(user_id), None)
            advanced._okai_last_cache_drop_reason = (
                f"BROKER_SWITCH_CACHE_DROP:{cached_broker}->{selected}"
            )
    except Exception:
        pass
    return selected


def apply_angel_pcr_force_binding() -> None:
    """Ensure Advanced AI uses the selected broker and PCR-recovered broker function."""
    try:
        _bind_selected_broker_source()
        from bot.angel_pcr_recovery_patch import apply_angel_pcr_recovery_patch
        from bot import broker_intelligence as broker
        from bot import advanced_intelligence_v2 as advanced

        try:
            apply_angel_pcr_recovery_patch()
        except Exception:
            pass

        if callable(getattr(broker, "get_broker_intelligence", None)):
            advanced.get_broker_intelligence = broker.get_broker_intelligence
            advanced._okai_angel_pcr_force_bound_v3 = True
    except Exception:
        pass


def apply_option_payload_pcr_injection_patch() -> None:
    """Recover PCR and keep option payload aligned with the selected broker."""
    try:
        _bind_selected_broker_source()
        from bot.angel_pcr_recovery_patch import (
            apply_angel_pcr_recovery_patch,
            recover_pcr_for_result,
        )
        from bot import advanced_intelligence_v2 as advanced

        if getattr(advanced, "_okai_option_payload_pcr_injection_v2", False):
            return

        try:
            apply_angel_pcr_recovery_patch()
        except Exception:
            pass

        original_option_payload = advanced._option_payload

        def option_payload_with_pcr(user_id, market):
            selected = _clear_advanced_option_cache_if_broker_changed(int(user_id))
            result = dict(original_option_payload(user_id, market) or {})
            result = recover_pcr_for_result(user_id, market or {}, result)
            if selected:
                result["selected_broker"] = selected
                result_broker = str(result.get("broker") or "").lower()
                if result_broker and result_broker != selected:
                    result["broker_mismatch"] = True
                    result["reason"] = (
                        f"SELECTED_BROKER_MISMATCH:{selected}!={result_broker}"
                    )
            return result

        advanced._option_payload = option_payload_with_pcr
        advanced._okai_option_payload_pcr_injection_v1 = True
        advanced._okai_option_payload_pcr_injection_v2 = True
    except Exception:
        pass


def apply_angel_timeout_last_good_cache_patch() -> None:
    """Keep last good option intelligence when Angel temporarily times out."""
    try:
        from bot import advanced_intelligence_v2 as advanced

        if getattr(advanced, "_okai_angel_timeout_last_good_cache_v1", False):
            return

        original_option_payload = advanced._option_payload

        def option_payload_with_timeout_cache(user_id, market):
            selected = _clear_advanced_option_cache_if_broker_changed(int(user_id))
            result = dict(original_option_payload(user_id, market) or {})
            success = bool(result.get("success"))
            reason = str(result.get("reason") or "")
            broker = str(result.get("broker") or "").lower()
            is_timeout = "TIMEOUT" in reason.upper() or "CONNECTTIMEOUT" in reason.upper()

            if success:
                if selected:
                    result["selected_broker"] = selected
                advanced._okai_last_good_option_payload = getattr(
                    advanced, "_okai_last_good_option_payload", {}
                )
                advanced._okai_last_good_option_payload[int(user_id)] = dict(result)
                return result

            cached_map = getattr(advanced, "_okai_last_good_option_payload", {}) or {}
            cached = dict(cached_map.get(int(user_id)) or {})
            cached_broker = str(cached.get("broker") or "").lower()
            if is_timeout and cached and (not selected or cached_broker == selected):
                option = dict(cached.get("option_intelligence") or {})
                reasons = list(option.get("reasons") or [])
                reasons.append("ANGEL_TIMEOUT_SHOWING_LAST_GOOD_OPTION_DATA")
                option["reasons"] = list(dict.fromkeys(reasons))[:12]
                cached["option_intelligence"] = option
                cached["stale_due_to_angel_timeout"] = True
                cached["reason"] = "ANGEL_TIMEOUT_SHOWING_LAST_GOOD_OPTION_DATA"
                if selected:
                    cached["selected_broker"] = selected
                return cached

            if broker == "angelone" and is_timeout:
                result["reason"] = "ANGEL_TIMEOUT_NO_LAST_GOOD_OPTION_DATA"
            if selected:
                result["selected_broker"] = selected
            return result

        advanced._option_payload = option_payload_with_timeout_cache
        advanced._okai_angel_timeout_last_good_cache_v1 = True
    except Exception:
        pass


def _option_for_display(raw_option: Dict[str, Any]) -> Dict[str, Any]:
    """Keep unavailable OI-derived fields from showing as fake precise values."""
    option = dict(raw_option or {})
    pcr_missing = option.get("pcr") is None
    pcr_unavailable = str(option.get("pcr_source") or "").upper() == "UNAVAILABLE"
    call_oi = _f(option.get("total_call_oi"), 0.0)
    put_oi = _f(option.get("total_put_oi"), 0.0)

    if pcr_missing and (pcr_unavailable or (call_oi <= 0 and put_oi <= 0)):
        option["max_pain"] = None
        option["max_pain_source"] = "OI_UNAVAILABLE"
        option["max_pain_unavailable_reason"] = "ANGEL_OI_MISSING"
        reasons = list(option.get("reasons") or [])
        reasons.append("MAX_PAIN_HIDDEN_BECAUSE_OI_UNAVAILABLE")
        option["reasons"] = list(dict.fromkeys(reasons))[:12]
    return option


def _live_probe_row(probe: Dict[str, Any]) -> Dict[str, Any]:
    option = _option_for_display(dict(probe.get("option_intelligence") or {}))
    selected = str(probe.get("selected_broker") or "").lower() or None
    broker = str(probe.get("broker") or selected or "").lower() or None
    reasons = []
    if probe.get("reason"):
        reasons.append(str(probe.get("reason")))
    reasons.extend(list(option.get("reasons") or []))
    reasons.append("LIVE_PROBE_CURRENT_MARKET")
    if selected:
        reasons.append("SELECTED_BROKER_" + selected.upper())
    if probe.get("broker_mismatch"):
        reasons.append("BROKER_CACHE_MISMATCH_CLEARED")
    if option.get("pcr_source"):
        reasons.append("PCR_SOURCE_" + str(option.get("pcr_source")))
    if option.get("pcr_error"):
        reasons.append("PCR_ERROR_" + str(option.get("pcr_error"))[:90])
    if option.get("max_pain_source") == "OI_UNAVAILABLE":
        reasons.append("MAX_PAIN_SOURCE_OI_UNAVAILABLE")

    return {
        "id": "LIVE_PROBE_DISPLAY_ONLY",
        "broker": broker,
        "selected_broker": selected,
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

        if getattr(advanced, "_okai_live_probe_recent_first_v3", False):
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
            if live_row.get("broker"):
                data["active_broker"] = live_row.get("broker")
            if live_row.get("selected_broker"):
                data["selected_broker"] = live_row.get("selected_broker")
            return data

        advanced.get_advanced_summary = get_advanced_summary
        advanced._okai_live_probe_recent_first_v1 = True
        advanced._okai_live_probe_recent_first_v2 = True
        advanced._okai_live_probe_recent_first_v3 = True
    except Exception:
        pass
