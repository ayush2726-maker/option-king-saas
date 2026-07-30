"""Selected-broker display guard for Advanced AI summary.

When the user switches broker, the bot runtime is intentionally stopped so old
sessions cannot keep trading. During that stopped interval Advanced AI can still
show the last stored snapshot from the previous broker. This display-only guard
prevents the mobile card from showing a stale broker after a switch.

It does not change entries, exits, quantities, risk, trade blocking, or order
execution.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _selected_broker_name(user_id: int) -> Optional[str]:
    try:
        from broker.selected_broker_control import selected_broker_name

        name = selected_broker_name(int(user_id))
        return str(name or "").lower().strip() or None
    except Exception:
        return None


def _row_broker(row: Dict[str, Any]) -> str:
    return str(
        row.get("selected_broker")
        or row.get("broker")
        or ""
    ).lower().strip()


def _placeholder_row(selected: str) -> Dict[str, Any]:
    return {
        "id": "SELECTED_BROKER_WAITING_FOR_LIVE_PROBE",
        "broker": selected,
        "selected_broker": selected,
        "symbol": None,
        "spot": 0,
        "advanced_decision": "COLLECTING",
        "advanced_confidence": 0,
        "advanced_probabilities": {},
        "option_decision": "NO_TRADE",
        "option_confidence": 0,
        "data_coverage_score": 0,
        "option_risk_score": 0,
        "option_summary": {
            "broker": selected,
            "pcr": None,
            "max_pain": None,
            "pcr_source": "WAITING_FOR_LIVE_PROBE",
            "reasons": [
                "SELECTED_BROKER_" + selected.upper(),
                "OLD_PREVIOUS_BROKER_AI_SNAPSHOT_HIDDEN",
                "START_BOT_TO_REFRESH_SELECTED_BROKER_AI",
            ],
        },
        "reasons": [
            "SELECTED_BROKER_" + selected.upper(),
            "OLD_PREVIOUS_BROKER_AI_SNAPSHOT_HIDDEN",
            "START_BOT_TO_REFRESH_SELECTED_BROKER_AI",
        ],
        "display_only": True,
        "live_probe": False,
        "trade_blocking": False,
        "order_execution": False,
    }


def apply_selected_broker_ai_summary_guard_patch() -> None:
    try:
        from bot import advanced_intelligence_v2 as advanced

        if getattr(advanced, "_okai_selected_broker_summary_guard_v1", False):
            return

        original_summary = advanced.get_advanced_summary

        def get_advanced_summary(user_id, recent_limit=20):
            data = dict(original_summary(user_id, recent_limit=recent_limit) or {})
            selected = _selected_broker_name(int(user_id))
            if not selected:
                return data

            data["selected_broker"] = selected

            recent = [
                dict(item)
                for item in list(data.get("recent_decisions") or [])
            ]
            current_probe = dict(data.get("current_probe") or {})
            active_broker = str(
                data.get("active_broker")
                or current_probe.get("selected_broker")
                or current_probe.get("broker")
                or (recent[0].get("selected_broker") if recent else "")
                or (recent[0].get("broker") if recent else "")
                or ""
            ).lower().strip()

            first_matches_selected = bool(recent and _row_broker(recent[0]) == selected)
            probe_matches_selected = bool(
                current_probe
                and str(
                    current_probe.get("selected_broker")
                    or current_probe.get("broker")
                    or ""
                ).lower().strip() == selected
            )

            if active_broker and active_broker != selected:
                data["stale_broker_hidden"] = active_broker
                data["active_broker"] = selected

            if not (first_matches_selected or probe_matches_selected):
                selected_rows = [
                    row for row in recent
                    if _row_broker(row) == selected
                ]
                limit = max(1, min(_i(recent_limit, 20), 50))
                data["recent_decisions"] = [
                    _placeholder_row(selected)
                ] + selected_rows[: max(0, limit - 1)]
                data["using_selected_broker_guard_for_display"] = True
                data["active_broker"] = selected
                return data

            data["active_broker"] = selected
            return data

        advanced.get_advanced_summary = get_advanced_summary
        advanced._okai_selected_broker_summary_guard_v1 = True
    except Exception:
        pass
