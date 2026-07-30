"""AI runtime hotfix bindings for live monitor display.

Monitoring/display only. These helpers do not change entries, exits, quantity,
SL, risk rules, trade blocking, or order execution.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


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


def _append_reason(option: Dict[str, Any], reason: str) -> None:
    reasons = list(option.get("reasons") or [])
    reasons.append(str(reason))
    option["reasons"] = list(dict.fromkeys(reasons))[:12]


def _inactive_broker_creds(user_id: int, broker_name: str) -> Optional[Dict[str, Any]]:
    """Read a saved broker credential for display-only OI fallback.

    The selected trading broker remains unchanged.  This only lets the AI monitor
    use another connected data source for OI-derived PCR/Max Pain when Angel does
    not provide OI.
    """
    try:
        from database import get_db
        from auth.utils import decrypt_credential

        conn = get_db()
        try:
            row = conn.execute(
                """
                SELECT * FROM broker_credentials
                WHERE user_id=? AND lower(broker_name)=?
                ORDER BY is_active DESC, last_connected DESC, id DESC
                LIMIT 1
                """,
                (int(user_id), str(broker_name).lower()),
            ).fetchone()
        finally:
            conn.close()

        if not row:
            return None
        return {
            "client_id": row["client_id"],
            "api_key": decrypt_credential(row["api_key"]),
            "password": decrypt_credential(row["api_secret"]),
            "totp_secret": decrypt_credential(row["totp_secret"])
            if row["totp_secret"]
            else None,
        }
    except Exception:
        return None


def _apply_upstox_oi_fallback(user_id: int, market: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Fill PCR/Max Pain from Upstox option-chain OI if Angel OI is missing.

    This is display/monitoring-only.  It does not route orders through Upstox and
    does not approve or block trades.
    """
    output = dict(result or {})
    option = dict(output.get("option_intelligence") or {})
    if not option:
        return output

    pcr_missing = option.get("pcr") is None
    max_pain_missing = option.get("max_pain") is None
    source_unavailable = str(option.get("pcr_source") or "").upper() == "UNAVAILABLE"
    if not (pcr_missing or max_pain_missing or source_unavailable):
        return output

    # Only use the alternate source when the active display payload is Angel and
    # Angel has told us OI/native PCR is unavailable.
    if str(output.get("broker") or "").lower() != "angelone":
        return output

    creds = _inactive_broker_creds(int(user_id), "upstox")
    if not creds:
        option["oi_fallback_source"] = "UNAVAILABLE"
        option["oi_fallback_error"] = "UPSTOX_CREDENTIALS_NOT_CONNECTED"
        _append_reason(option, "OI_FALLBACK_UPSTOX_NOT_CONNECTED")
        output["option_intelligence"] = option
        return output

    try:
        from bot import broker_intelligence as broker

        underlying = str(
            output.get("underlying")
            or market.get("symbol")
            or market.get("underlying")
            or "NIFTY"
        ).upper()
        spot = _f(output.get("spot") or market.get("price"), 0.0)
        if spot <= 0:
            raise RuntimeError("INVALID_SPOT_FOR_UPSTOX_OI_FALLBACK")

        obj = broker._get_multi_session(int(user_id), "upstox", creds)
        raw_chain = broker._fetch_upstox(obj, underlying, spot, broker.DEFAULT_WINGS)
        summary = broker.summarize_chain(raw_chain, {})

        pcr = summary.get("pcr")
        max_pain = summary.get("max_pain")
        if pcr is None and max_pain is None:
            raise RuntimeError("UPSTOX_OI_FALLBACK_EMPTY")

        if pcr is not None:
            option["pcr"] = pcr
            option["pcr_source"] = "UPSTOX_CHAIN_OI_FALLBACK"
            option["pcr_recovered"] = True
            option.pop("pcr_error", None)
        if max_pain is not None:
            option["max_pain"] = max_pain
            option["max_pain_source"] = "UPSTOX_CHAIN_OI_FALLBACK"
            option.pop("max_pain_unavailable_reason", None)
        option["total_call_oi"] = summary.get("total_call_oi", option.get("total_call_oi"))
        option["total_put_oi"] = summary.get("total_put_oi", option.get("total_put_oi"))
        option["oi_fallback_source"] = "UPSTOX_OPTION_CHAIN"
        option["oi_fallback_underlying"] = underlying
        _append_reason(option, "PCR_MAX_PAIN_FROM_UPSTOX_OI_FALLBACK")
        output["option_intelligence"] = option
        output["oi_fallback_used"] = True
        output["oi_fallback_source"] = "UPSTOX_OPTION_CHAIN"
        return output
    except Exception as exc:
        option["oi_fallback_source"] = "UPSTOX_OPTION_CHAIN"
        option["oi_fallback_error"] = f"{type(exc).__name__}:{str(exc)[:180]}"
        _append_reason(option, "UPSTOX_OI_FALLBACK_FAILED")
        output["option_intelligence"] = option
        return output


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
    the final payload returned to the monitor, then tries a connected Upstox OI
    fallback for display-only PCR/Max Pain when Angel OI is unavailable.
    """
    try:
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
            result = dict(original_option_payload(user_id, market) or {})
            recovered = recover_pcr_for_result(user_id, market or {}, result)
            return _apply_upstox_oi_fallback(user_id, dict(market or {}), recovered)

        advanced._option_payload = option_payload_with_pcr
        advanced._okai_option_payload_pcr_injection_v1 = True
        advanced._okai_option_payload_pcr_injection_v2 = True
    except Exception:
        pass


def apply_angel_timeout_last_good_cache_patch() -> None:
    """Keep last good option intelligence when Angel temporarily times out.

    Angel LTP/market-data timeouts should not overwrite the Advanced-AI card with
    0% coverage. This is display-only and does not approve trades or orders.
    """
    try:
        from bot import advanced_intelligence_v2 as advanced

        if getattr(advanced, "_okai_angel_timeout_last_good_cache_v1", False):
            return

        original_option_payload = advanced._option_payload

        def option_payload_with_timeout_cache(user_id, market):
            result = dict(original_option_payload(user_id, market) or {})
            success = bool(result.get("success"))
            reason = str(result.get("reason") or "")
            broker = str(result.get("broker") or "").lower()
            is_timeout = "TIMEOUT" in reason.upper() or "CONNECTTIMEOUT" in reason.upper()

            if success:
                advanced._okai_last_good_option_payload = getattr(
                    advanced, "_okai_last_good_option_payload", {}
                )
                advanced._okai_last_good_option_payload[int(user_id)] = dict(result)
                return result

            cached_map = getattr(advanced, "_okai_last_good_option_payload", {}) or {}
            cached = dict(cached_map.get(int(user_id)) or {})
            if is_timeout and cached:
                option = dict(cached.get("option_intelligence") or {})
                _append_reason(option, "ANGEL_TIMEOUT_SHOWING_LAST_GOOD_OPTION_DATA")
                cached["option_intelligence"] = option
                cached["stale_due_to_angel_timeout"] = True
                cached["reason"] = "ANGEL_TIMEOUT_SHOWING_LAST_GOOD_OPTION_DATA"
                return cached

            if broker == "angelone" and is_timeout:
                result["reason"] = "ANGEL_TIMEOUT_NO_LAST_GOOD_OPTION_DATA"
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
    has_oi_fallback = option.get("oi_fallback_source") == "UPSTOX_OPTION_CHAIN"

    if pcr_missing and not has_oi_fallback and (pcr_unavailable or (call_oi <= 0 and put_oi <= 0)):
        # Max Pain is also OI-derived.  If Angel did not provide CE/PE OI,
        # keeping a calculated strike on screen is misleading.  Show -- and
        # expose the diagnostic in the reason line.
        option["max_pain"] = None
        option["max_pain_source"] = "OI_UNAVAILABLE"
        option["max_pain_unavailable_reason"] = "ANGEL_OI_MISSING"
        _append_reason(option, "MAX_PAIN_HIDDEN_BECAUSE_OI_UNAVAILABLE")
    return option


def _live_probe_row(probe: Dict[str, Any]) -> Dict[str, Any]:
    option = _option_for_display(dict(probe.get("option_intelligence") or {}))
    reasons = []
    if probe.get("reason"):
        reasons.append(str(probe.get("reason")))
    reasons.extend(list(option.get("reasons") or []))
    reasons.append("LIVE_PROBE_CURRENT_MARKET")
    if option.get("pcr_source"):
        reasons.append("PCR_SOURCE_" + str(option.get("pcr_source")))
    if option.get("pcr_error"):
        reasons.append("PCR_ERROR_" + str(option.get("pcr_error"))[:90])
    if option.get("max_pain_source") == "OI_UNAVAILABLE":
        reasons.append("MAX_PAIN_SOURCE_OI_UNAVAILABLE")
    if option.get("oi_fallback_source"):
        reasons.append("OI_FALLBACK_" + str(option.get("oi_fallback_source")))
    if option.get("oi_fallback_error"):
        reasons.append("OI_FALLBACK_ERROR_" + str(option.get("oi_fallback_error"))[:90])

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
            if probe.get("broker"):
                data["active_broker"] = probe.get("broker")
            return data

        advanced.get_advanced_summary = get_advanced_summary
        advanced._okai_live_probe_recent_first_v1 = True
        advanced._okai_live_probe_recent_first_v2 = True
        advanced._okai_live_probe_recent_first_v3 = True
    except Exception:
        pass
