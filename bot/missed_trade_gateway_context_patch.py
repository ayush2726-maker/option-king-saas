"""Display-only context for historical gateway blocks in missed-trade reports.

Missed-trade rows preserve the execution reason captured at that candle. A row
captured while the local gateway was offline must not be presented as if the
gateway is offline *now*. This patch leaves stored/training reasons untouched,
but annotates the API response with current gateway status and clearer wording.
"""

from __future__ import annotations

from typing import Any

VERSION = "MISSED-TRADE-GATEWAY-CONTEXT-V1"
_INSTALLED = False


def _display_reason(value: Any) -> str:
    text = str(value or "")
    upper = text.strip().upper()
    if upper == "LOCAL_GATEWAY_OFFLINE":
        return "LOCAL GATEWAY WAS OFFLINE AT CAPTURE"
    if upper == "LOCAL_GATEWAY_NOT_PAIRED":
        return "LOCAL GATEWAY WAS NOT PAIRED AT CAPTURE"
    if upper == "LOCAL_GATEWAY_DISABLED":
        return "LOCAL GATEWAY WAS DISABLED AT CAPTURE"
    if upper == "LOCAL_GATEWAY_NOT_ARMED":
        return "LOCAL GATEWAY WAS NOT ARMED AT CAPTURE"
    if upper == "STATIC_IP_MISMATCH":
        return "STATIC IP MISMATCH AT CAPTURE"
    return text


def _decorate_report(report: Any, user_id: int):
    if not isinstance(report, dict):
        return report

    try:
        from local_gateway.service import get_gateway_status
        gateway = dict(get_gateway_status(int(user_id)) or {})
    except Exception:
        gateway = {}

    report = dict(report)
    report["gateway_status_now"] = {
        "paired": bool(gateway.get("paired")),
        "online": bool(gateway.get("online")),
        "enabled": bool(gateway.get("enabled")),
        "server_armed": bool(gateway.get("server_armed")),
        "last_seen_at": gateway.get("last_seen_at"),
        "agent_version": gateway.get("agent_version"),
        "context": "CURRENT_STATUS_NOT_HISTORICAL_REASON",
    }
    report["gateway_reason_context_version"] = VERSION

    # The canonical stored reason is copied before changing only the response
    # wording. AI/training storage is never modified by this patch.
    for key in ("recent", "items", "trades", "missed_trades"):
        rows = report.get(key)
        if not isinstance(rows, list):
            continue
        decorated = []
        for raw in rows:
            if not isinstance(raw, dict):
                decorated.append(raw)
                continue
            item = dict(raw)
            reasons = item.get("block_reasons")
            if isinstance(reasons, list):
                canonical = [str(reason or "") for reason in reasons]
                item["canonical_block_reasons"] = canonical
                item["block_reasons"] = [_display_reason(reason) for reason in canonical]
                item["gateway_reason_is_historical"] = any(
                    reason.strip().upper() in {
                        "LOCAL_GATEWAY_OFFLINE",
                        "LOCAL_GATEWAY_NOT_PAIRED",
                        "LOCAL_GATEWAY_DISABLED",
                        "LOCAL_GATEWAY_NOT_ARMED",
                        "STATIC_IP_MISMATCH",
                    }
                    for reason in canonical
                )
                item["gateway_online_now"] = bool(gateway.get("online"))
                item["gateway_last_seen_at_now"] = gateway.get("last_seen_at")
            decorated.append(item)
        report[key] = decorated
    return report


def apply_missed_trade_gateway_context_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    # ai_routes imported get_missed_trade_summary directly, so patch that bound
    # name as well as the source module function.
    from bot import ai_routes
    from bot import missed_trade_learning_v1 as missed

    original = ai_routes.get_missed_trade_summary
    if getattr(original, "_okai_gateway_context_v1", False):
        _INSTALLED = True
        return

    def summary_with_gateway_context(user_id: int, recent_limit: int = 20):
        return _decorate_report(
            original(user_id, recent_limit=recent_limit),
            int(user_id),
        )

    summary_with_gateway_context._okai_gateway_context_v1 = True
    ai_routes.get_missed_trade_summary = summary_with_gateway_context
    missed.get_missed_trade_summary = summary_with_gateway_context
    _INSTALLED = True
