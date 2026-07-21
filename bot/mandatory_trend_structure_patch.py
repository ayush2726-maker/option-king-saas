"""Mandatory trend-structure gate for OKAI AUTO entries.

The protected score threshold remains 82.  A trade may qualify only when all
three primary directional confirmations agree with the candidate:

- VWAP direction
- Supertrend direction
- EMA9/EMA21 trend direction

ORB and two-candle momentum remain scoring/quality confirmations, but are not
mandatory.  Existing reversal, ATR-extension, anti-chase, sideways and option
premium guards remain active.
"""

from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime
from bot import routes
from bot import strategy

MANDATORY_KEYS = ("vwap", "supertrend", "ema_trend")


def _number(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _checks(signal, market):
    candidate = str(signal.get("candidate_signal") or "WAIT").upper()
    price = _number(market.get("price"), 0)
    vwap = _number(market.get("vwap"), price)
    ema9 = _number(market.get("ema9"), price)
    ema21 = _number(market.get("ema21"), price)
    supertrend = str(market.get("supertrend_dir") or "NEUTRAL").upper()
    trend = str(market.get("trend") or "SIDEWAYS").upper()

    if candidate == "CE":
        values = {
            "vwap": price > vwap,
            "supertrend": supertrend == "UP",
            "ema_trend": ema9 > ema21 and trend == "UPTREND",
        }
    elif candidate == "PE":
        values = {
            "vwap": price < vwap,
            "supertrend": supertrend == "DOWN",
            "ema_trend": ema9 < ema21 and trend == "DOWNTREND",
        }
    else:
        values = {key: False for key in MANDATORY_KEYS}

    return candidate, values


def _clean_reason(reason):
    text = str(reason or "").strip()
    if not text:
        return ""
    if text.startswith("CORE_CONFIRMATIONS_"):
        return ""
    if text == "CORE_CONFIRMATIONS_BELOW_4":
        return ""
    return text


def _normalize(signal, market):
    if not isinstance(signal, dict):
        return signal

    output = dict(signal)
    candidate, mandatory = _checks(output, market or {})
    score = int(round(_number(output.get("score"), 0)))
    minimum = int(round(_number(output.get("min_score", 82), 82)))

    missing = [key for key in MANDATORY_KEYS if not mandatory.get(key)]
    mandatory_reasons = {
        "vwap": "VWAP_DIRECTION_REQUIRED",
        "supertrend": "SUPERTREND_DIRECTION_REQUIRED",
        "ema_trend": "EMA_TREND_REQUIRED",
    }

    fresh_reasons = []
    for reason in output.get("fresh_entry_block_reasons") or []:
        cleaned = _clean_reason(reason)
        if cleaned and cleaned not in fresh_reasons:
            fresh_reasons.append(cleaned)

    safety_reasons = []
    for reason in output.get("safety_gate_reasons") or []:
        cleaned = _clean_reason(reason)
        if cleaned and cleaned not in safety_reasons:
            safety_reasons.append(cleaned)

    if candidate not in ("CE", "PE"):
        safety_reasons.append("NO_DIRECTIONAL_SIGNAL")
    if score < minimum:
        safety_reasons.append(f"SCORE_BELOW_{minimum}")
    for key in missing:
        safety_reasons.append(mandatory_reasons[key])

    for reason in fresh_reasons:
        if reason not in safety_reasons:
            safety_reasons.append(reason)

    if output.get("sideways_blocked") and "SIDEWAYS_BLOCKED" not in safety_reasons:
        safety_reasons.append("SIDEWAYS_BLOCKED")
    if output.get("ema_chase_blocked") and "EMA_ANTI_CHASE" not in safety_reasons:
        safety_reasons.append("EMA_ANTI_CHASE")
    if output.get("vwap_chase_blocked") and "VWAP_ANTI_CHASE" not in safety_reasons:
        safety_reasons.append("VWAP_ANTI_CHASE")
    if (
        output.get("chase_blocked")
        and not output.get("ema_chase_blocked")
        and not output.get("vwap_chase_blocked")
        and "CHASE_GUARD_BLOCKED" not in safety_reasons
    ):
        safety_reasons.append("CHASE_GUARD_BLOCKED")

    safety_reasons = list(dict.fromkeys(safety_reasons))
    mandatory_passed = not missing
    eligible = bool(
        candidate in ("CE", "PE")
        and score >= minimum
        and mandatory_passed
        and not safety_reasons
    )

    all_directional = dict(output.get("directional_checks") or {})
    all_directional.update(mandatory)
    actual_core = sum(1 for passed in all_directional.values() if passed)

    output.update({
        "signal": candidate if eligible else "WAIT",
        "candidate_signal": candidate,
        "trade_allowed": eligible,
        "mandatory_confirmations": mandatory,
        "mandatory_confirmation_keys": list(MANDATORY_KEYS),
        "mandatory_confirmations_passed": mandatory_passed,
        "missing_mandatory_confirmations": missing,
        "structure_confirmations": sum(1 for passed in mandatory.values() if passed),
        "structure_required": 3,
        "core_confirmations": actual_core,
        "core_required": 3,
        "directional_checks": all_directional,
        "fresh_entry_block_reasons": fresh_reasons,
        "fresh_entry_ok": not fresh_reasons,
        "safety_gate_reasons": safety_reasons,
        "safety_gate_passed": eligible,
        "entry_timing_mode": "MANDATORY_VWAP_ST_EMA_V1",
    })
    return output


def _repair_scan(scan):
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan
    market = scan.get("market_data") or {}
    signal = scan.get("signal_data") or {}
    scan["signal_data"] = _normalize(signal, market)
    scan["market_data"]["signal"] = scan["signal_data"].get("signal", "WAIT")
    scan["market_data"]["signal_score"] = scan["signal_data"].get("score", 0)
    return scan


def _summary(original, scan):
    summary = dict(original(scan) or {})
    signal = scan.get("signal_data") or {}
    reasons = list(signal.get("safety_gate_reasons") or [])

    summary["trade_allowed"] = bool(signal.get("trade_allowed", False))
    summary["signal"] = signal.get("signal", "WAIT")
    summary["candidate_signal"] = signal.get("candidate_signal", "WAIT")
    summary["mandatory_confirmations"] = signal.get("mandatory_confirmations")
    summary["missing_mandatory_confirmations"] = signal.get(
        "missing_mandatory_confirmations", []
    )
    summary["structure_confirmations"] = signal.get("structure_confirmations")
    summary["structure_required"] = 3
    summary["core_required"] = 3
    summary["safety_gate_reasons"] = reasons[:8]

    if summary["trade_allowed"]:
        summary["status"] = "QUALIFIED"
        summary["entry_status"] = "QUALIFIED"
        summary["entry_block_reason"] = None
    elif int(summary.get("score") or 0) >= int(summary.get("min_score") or 82):
        reason = reasons[0] if reasons else "MANDATORY_STRUCTURE_NOT_QUALIFIED"
        summary["status"] = "SAFETY_BLOCKED"
        summary["candidate_signal"] = str(reason)[:90]
        summary["entry_block_reason"] = str(reason)[:180]

    return summary


def apply_mandatory_trend_structure_patch():
    if getattr(strategy, "_okai_mandatory_structure_v1", False):
        return

    original_signal = angel_fetcher.get_full_signal

    def mandatory_signal(market_data, consecutive_losses=0, profile=None):
        result = original_signal(
            market_data,
            consecutive_losses=consecutive_losses,
            profile=profile,
        )
        return _normalize(result, market_data)

    strategy.get_full_signal = mandatory_signal
    angel_fetcher.get_full_signal = mandatory_signal
    routes.get_full_signal = mandatory_signal

    original_scan_angel = runtime._scan_angel
    original_scan_multi = runtime._scan_multi

    def scan_angel(*args, **kwargs):
        return [_repair_scan(scan) for scan in original_scan_angel(*args, **kwargs)]

    def scan_multi(*args, **kwargs):
        return [_repair_scan(scan) for scan in original_scan_multi(*args, **kwargs)]

    runtime._scan_angel = scan_angel
    runtime._scan_multi = scan_multi

    original_summary = runtime._summary
    runtime._summary = lambda scan: _summary(original_summary, scan)

    strategy._okai_mandatory_structure_v1 = True
