"""Anti-chase consistency V3.

Feed Safety Consistency V2 derived missing directional fields for replay/live
scans, but it accidentally reintroduced the retired 0.95 ATR EMA and 2.20 ATR
VWAP gates. Those duplicate gates could block a 100/82 setup even after the
shared strategy anti-chase logic had already passed it.

This patch removes only those two legacy duplicate reasons. The real shared
anti-chase gate remains active with the strategy's point + ATR-adaptive limits,
along with core confirmation, reversal candle, ORB extension, late exhaustion,
and sideways protection.
"""

from bot import feed_safety_consistency_patch as consistency
from bot import strategy


LEGACY_DUPLICATE_REASONS = {
    "EMA_EXTENSION_OVER_0.95_ATR",
    "VWAP_EXTENSION_OVER_2.20_ATR",
}


def _without_legacy_reasons(values):
    return [
        value
        for value in (values or [])
        if str(value or "").strip() not in LEGACY_DUPLICATE_REASONS
    ]


def apply_anti_chase_consistency_v3_patch():
    if getattr(strategy, "_okai_anti_chase_consistency_v3", False):
        return

    original_directional_snapshot = consistency._directional_snapshot
    original_safety_reasons = consistency._safety_reasons
    original_normalize_signal = consistency._normalize_signal
    original_reason_summary = consistency._reason_summary

    def directional_snapshot_v3(result, market_data):
        snapshot = dict(original_directional_snapshot(result, market_data) or {})
        snapshot["fresh_reasons"] = _without_legacy_reasons(
            snapshot.get("fresh_reasons")
        )
        snapshot["shared_anti_chase_only"] = True
        return snapshot

    def safety_reasons_v3(result, market_data=None):
        reasons = original_safety_reasons(result, market_data)
        return list(dict.fromkeys(_without_legacy_reasons(reasons)))

    def normalize_signal_v3(result, market_data):
        if not isinstance(result, dict):
            return result

        cleaned = dict(result)
        if "fresh_entry_block_reasons" in cleaned:
            cleaned["fresh_entry_block_reasons"] = _without_legacy_reasons(
                cleaned.get("fresh_entry_block_reasons")
            )
            cleaned["fresh_entry_ok"] = not cleaned[
                "fresh_entry_block_reasons"
            ]

        normalized = original_normalize_signal(cleaned, market_data)
        if isinstance(normalized, dict):
            normalized["fresh_entry_block_reasons"] = _without_legacy_reasons(
                normalized.get("fresh_entry_block_reasons")
            )
            normalized["safety_gate_reasons"] = _without_legacy_reasons(
                normalized.get("safety_gate_reasons")
            )
            normalized["fresh_entry_ok"] = not normalized[
                "fresh_entry_block_reasons"
            ]
            normalized["shared_anti_chase_only"] = True
            normalized["legacy_duplicate_atr_gate_removed"] = True
        return normalized

    def reason_summary_v3(original, scan):
        summary = dict(original_reason_summary(original, scan) or {})
        reasons = _without_legacy_reasons(
            summary.get("safety_gate_reasons")
        )
        summary["safety_gate_reasons"] = reasons
        summary["legacy_duplicate_atr_gate_removed"] = True

        old_reason = str(summary.get("entry_block_reason") or "")
        old_candidate = str(summary.get("candidate_signal") or "")
        if old_reason in LEGACY_DUPLICATE_REASONS:
            summary["entry_block_reason"] = reasons[0] if reasons else None
        if old_candidate in LEGACY_DUPLICATE_REASONS:
            signal = (scan.get("signal_data") or {}).get(
                "candidate_signal",
                "WAIT",
            )
            summary["candidate_signal"] = signal

        if (
            int(summary.get("score") or 0)
            >= int(summary.get("min_score") or 82)
            and bool((scan.get("signal_data") or {}).get("trade_allowed"))
        ):
            summary["status"] = "QUALIFIED"
            summary["entry_block_reason"] = None
        return summary

    consistency._directional_snapshot = directional_snapshot_v3
    consistency._safety_reasons = safety_reasons_v3
    consistency._normalize_signal = normalize_signal_v3
    consistency._reason_summary = reason_summary_v3

    strategy._okai_anti_chase_consistency_v3 = True
