"""Attach explicit trade-miss diagnostics to live AUTO scan payloads.

This is an observability patch.  It does not change entry, exit, order,
quantity, SL, cooldown, or risk decisions.  Its purpose is to make tomorrow's
live run auditable from the mobile Trade tab: if a strong-looking market is
blocked, the scan tells us whether the block came from score, structure,
time cutoff, cooldown, fresh-entry guard, or display/engine mismatch.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from bot import auto_portfolio_runtime as runtime


ENTRY_CUTOFF_HOUR = 15
ENTRY_CUTOFF_MINUTE = 25


def _f(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _i(value, default=0):
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _b(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _now_ist():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _after_entry_cutoff():
    now = _now_ist()
    return (now.hour, now.minute) >= (ENTRY_CUTOFF_HOUR, ENTRY_CUTOFF_MINUTE)


def _append_unique(items, value, limit=10):
    text = str(value or "").strip()
    if not text:
        return items
    if text not in items:
        items.append(text[:180])
    return items[:limit]


def _reasons(signal):
    signal = signal if isinstance(signal, dict) else {}
    reasons = []
    for key in (
        "entry_block_reason",
        "safety_gate_reasons",
        "fresh_entry_block_reasons",
        "missing_mandatory_confirmations",
        "warnings",
    ):
        value = signal.get(key)
        if isinstance(value, list):
            for item in value:
                _append_unique(reasons, item, limit=12)
        else:
            _append_unique(reasons, value, limit=12)
    return reasons


def _first_block_reason(signal, min_score):
    signal = signal if isinstance(signal, dict) else {}
    score = _i(signal.get("score"), 0)
    reasons = _reasons(signal)
    if score < min_score:
        return f"SCORE_BELOW_{min_score}"
    if reasons:
        return str(reasons[0])[:80]
    if _after_entry_cutoff():
        return "ENTRY_CUTOFF_AFTER_15_25"
    return "UNKNOWN_BLOCK_REASON"


def _active_score(scan, signal):
    payload = None
    if isinstance(signal, dict):
        payload = signal.get("live_score_breakdown")
    if not isinstance(payload, dict) and isinstance(scan, dict):
        payload = scan.get("live_score_breakdown")
    payload = payload if isinstance(payload, dict) else {}
    display = _i(payload.get("display_score", payload.get("score", signal.get("score", 0))), 0)
    decision = _i(payload.get("decision_score", signal.get("score", display)), display)
    return display, decision


def _component_map(signal):
    payload = signal.get("live_score_breakdown") if isinstance(signal, dict) else None
    components = []
    if isinstance(payload, dict):
        components = payload.get("components") or []
    if not components and isinstance(signal, dict):
        components = signal.get("score_components") or []
    result = {}
    for row in components if isinstance(components, list) else []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if key:
            result[key] = row
    return result


def _strong_trend_side(market, signal):
    market = market if isinstance(market, dict) else {}
    signal = signal if isinstance(signal, dict) else {}
    candidate = str(signal.get("candidate_signal") or signal.get("signal") or "WAIT").upper()
    if candidate not in {"CE", "PE"}:
        return "WAIT"

    price = _f(market.get("price"), 0)
    vwap = _f(market.get("vwap"), price)
    ema9 = _f(market.get("ema9"), price)
    ema21 = _f(market.get("ema21"), price)
    trend = str(market.get("trend") or "SIDEWAYS").upper()
    st = str(market.get("supertrend_dir") or "NEUTRAL").upper()
    adx = _f(market.get("adx"), 0)
    adx_threshold = _f(signal.get("adx_threshold"), 22.0)

    if candidate == "CE":
        ok = price > vwap and ema9 > ema21 and trend == "UPTREND" and st == "UP" and adx >= adx_threshold
        return "CE" if ok else "WAIT"
    ok = price < vwap and ema9 < ema21 and trend == "DOWNTREND" and st == "DOWN" and adx >= adx_threshold
    return "PE" if ok else "WAIT"


def _audit_lines(scan):
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return []

    signal = scan.get("signal_data") if isinstance(scan.get("signal_data"), dict) else {}
    market = scan.get("market_data") if isinstance(scan.get("market_data"), dict) else {}
    underlying = str(scan.get("underlying") or "INDEX").upper()
    candidate = str(signal.get("candidate_signal") or signal.get("signal") or "WAIT").upper()
    engine_signal = str(signal.get("signal") or "WAIT").upper()
    trade_allowed = _b(signal.get("trade_allowed"), False)
    min_score = _i(signal.get("min_score", signal.get("min_score_required", 82)), 82)
    display_score, decision_score = _active_score(scan, signal)
    reason = _first_block_reason(signal, min_score) if not trade_allowed else "QUALIFIED"

    lines = []
    _append_unique(
        lines,
        f"AUDIT: {underlying} CANDIDATE {candidate} ENGINE {engine_signal} DISPLAY {display_score}/{min_score} DECISION {decision_score}/{min_score}",
        limit=8,
    )

    if not trade_allowed:
        _append_unique(lines, f"BLOCK_REASON: {reason}", limit=8)
        if display_score >= min_score or decision_score >= min_score:
            _append_unique(lines, f"MISSED_SIGNAL_AUDIT: SCORE_OK_BUT_BLOCKED_BY {reason}", limit=8)
    else:
        _append_unique(lines, "ENTRY_READY_AUDIT: QUALIFIED_BY_ENGINE", limit=8)

    if _after_entry_cutoff():
        _append_unique(lines, "TIME_GATE: AFTER_15_25_NORMAL_ENTRY_BLOCK", limit=8)

    strong_side = _strong_trend_side(market, signal)
    if strong_side in {"CE", "PE"}:
        _append_unique(lines, f"STRONG_TREND_DAY: {strong_side}_VWAP_EMA_ST_ADX_ALIGNED", limit=8)

    components = _component_map(signal)
    failed = [
        key.upper()
        for key, row in components.items()
        if _i(row.get("max_score"), 0) > 0 and not _b(row.get("passed"), False)
    ]
    if failed and not trade_allowed:
        _append_unique(lines, "FAILED_COMPONENTS: " + ",".join(failed[:5]), limit=8)

    return lines[:8]


def _attach_audit(scan):
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan
    signal = scan.get("signal_data")
    if not isinstance(signal, dict):
        return scan

    warnings = list(signal.get("warnings") or [])
    for line in _audit_lines(scan):
        _append_unique(warnings, line, limit=12)
    signal["warnings"] = warnings[:12]

    payload = signal.get("live_score_breakdown")
    if isinstance(payload, dict):
        existing = list(payload.get("warnings") or [])
        for line in _audit_lines(scan):
            _append_unique(existing, line, limit=12)
        payload["warnings"] = existing[:12]
        signal["live_score_breakdown"] = payload
        scan["live_score_breakdown"] = payload
    return scan


def apply_trade_miss_audit_patch() -> None:
    if getattr(runtime, "_okai_trade_miss_audit_v1", False):
        return

    original_build_scan = runtime._build_scan
    original_summary = runtime._summary

    def build_scan_with_trade_audit(user_id, underlying, df, profile, loss_streak):
        scan = original_build_scan(user_id, underlying, df, profile, loss_streak)
        try:
            return _attach_audit(scan)
        except Exception as exc:
            try:
                print(f"Trade miss audit attach skipped: {str(exc)[:160]}")
            except Exception:
                pass
            return scan

    def summary_with_trade_audit(scan):
        try:
            _attach_audit(scan)
        except Exception:
            pass
        data = original_summary(scan)
        if isinstance(data, dict) and isinstance(scan, dict):
            signal = scan.get("signal_data") if isinstance(scan.get("signal_data"), dict) else {}
            warnings = list(data.get("warnings") or [])
            for line in _audit_lines(scan):
                _append_unique(warnings, line, limit=12)
            data["warnings"] = warnings[:12]
            if signal:
                data["entry_block_reason"] = _first_block_reason(
                    signal,
                    _i(signal.get("min_score", signal.get("min_score_required", 82)), 82),
                )
                data["engine_signal"] = str(signal.get("signal") or "WAIT").upper()
                data["candidate_signal"] = str(signal.get("candidate_signal") or data.get("candidate_signal") or "WAIT").upper()
        return data

    runtime._build_scan = build_scan_with_trade_audit
    runtime._summary = summary_with_trade_audit
    runtime._okai_trade_miss_audit_v1 = True
