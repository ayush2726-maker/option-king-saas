"""Upstox institutional/analytics overlay for broker-neutral advanced AI.

Angel One and Zerodha keep their full option intelligence and receive an
explicit neutral institutional fallback. This patch is shadow-only.
"""
from __future__ import annotations

import math
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping

VERSION = "OKAI-INSTITUTIONAL-FLOW-OVERLAY-V1"
_CACHE_SECONDS = 300
_lock = threading.RLock()
_cache: Dict[int, Dict[str, Any]] = {}
_installed = False


def _f(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else float(default)
    except Exception:
        return float(default)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _latest(rows):
    usable = [dict(row) for row in (rows or []) if isinstance(row, Mapping)]
    if not usable:
        return {}
    return max(
        usable,
        key=lambda row: _f(
            row.get("time_stamp") or row.get("timestamp") or row.get("date"),
            0,
        ),
    )


def _flow_summary(fii_data, dii_data):
    amount = 0.0
    contracts = 0.0
    breakdown = {}
    for owner, payload in (("FII", fii_data), ("DII", dii_data)):
        if not isinstance(payload, Mapping):
            continue
        for segment, rows in payload.items():
            row = _latest(rows)
            if not row:
                continue
            net_amount = _f(row.get("buy_amount")) - _f(row.get("sell_amount"))
            net_contracts = (
                _f(row.get("long_contracts")) - _f(row.get("short_contracts"))
            )
            if not net_contracts:
                net_contracts = (
                    _f(row.get("buy_contracts"))
                    - _f(row.get("sell_contracts"))
                )
            amount += net_amount
            contracts += net_contracts
            breakdown[f"{owner}:{segment}"] = {
                "net_amount": round(net_amount, 4),
                "net_contracts": round(net_contracts, 2),
                "timestamp": row.get("time_stamp") or row.get("timestamp"),
            }
    amount_score = (
        math.copysign(min(45.0, math.log10(abs(amount) + 1.0) * 8.0), amount)
        if amount
        else 0.0
    )
    contract_score = (
        math.copysign(
            min(55.0, math.log10(abs(contracts) + 1.0) * 10.0),
            contracts,
        )
        if contracts
        else 0.0
    )
    score = _clamp(amount_score + contract_score, -100.0, 100.0)
    direction = "CE" if score >= 15 else "PE" if score <= -15 else "NEUTRAL"
    confidence = int(_clamp(45 + abs(score) * 0.42, 45, 78))
    return {
        "available": bool(breakdown),
        "source": "UPSTOX_FII_DII_ANALYTICS",
        "direction": direction,
        "confidence": confidence,
        "score": round(score, 2),
        "net_amount": round(amount, 4),
        "net_contracts": round(contracts, 2),
        "breakdown": breakdown,
    }


def _get_json(obj, endpoint, params):
    import requests

    response = requests.get(
        f"https://api.upstox.com/v2/market/{endpoint}",
        params=params,
        headers=obj._h(),
        timeout=8,
    )
    payload = response.json()
    if response.status_code != 200 or payload.get("status") != "success":
        return None, str(payload.get("errors") or payload)[:180]
    return payload.get("data"), None


def _fetch(user_id, broker_module, option_summary):
    broker_name, creds = broker_module._get_active_broker(int(user_id))
    broker = str(broker_name or "").lower()
    if broker != "upstox":
        return {
            "available": False,
            "source": "NEUTRAL_FALLBACK",
            "broker": broker,
            "direction": "NEUTRAL",
            "confidence": 0,
            "reason": "NATIVE_FII_DII_NOT_AVAILABLE_FOR_ACTIVE_BROKER",
        }
    current = time.monotonic()
    with _lock:
        cached = _cache.get(int(user_id))
        if cached and current - cached["mono"] < _CACHE_SECONDS:
            return dict(cached["value"])
    obj = broker_module._get_multi_session(int(user_id), "upstox", creds)
    underlying = str(option_summary.get("underlying") or "NIFTY").upper()
    instrument_key = broker_module.UPSTOX_UNDERLYING_KEYS.get(underlying)
    expiry = str(option_summary.get("expiry") or "")[:10]
    today = (
        datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    ).date().isoformat()
    errors = []
    fii, error = _get_json(
        obj,
        "fii",
        {
            "data_type": (
                "NSE_FO|INDEX_FUTURES,NSE_FO|INDEX_OPTIONS,NSE_EQ|CASH"
            ),
            "interval": "1D",
        },
    )
    if error:
        errors.append("FII:" + error)
    dii, error = _get_json(
        obj,
        "dii",
        {"data_type": "NSE_EQ|CASH", "interval": "1D"},
    )
    if error:
        errors.append("DII:" + error)
    flow = _flow_summary(fii, dii)
    analytics = {}
    if instrument_key and expiry:
        requests_to_make = (
            ("oi", "oi", {
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": today,
            }),
            ("change_oi", "change-oi", {
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": today,
                "interval": 1,
            }),
            ("pcr", "pcr", {
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": today,
                "bucket_interval": 60,
            }),
            ("max_pain", "max-pain", {
                "instrument_key": instrument_key,
                "expiry": expiry,
                "date": today,
                "bucket_interval": 60,
            }),
        )
        for name, endpoint, params in requests_to_make:
            data, error = _get_json(obj, endpoint, params)
            if data is not None:
                analytics[name] = data
            elif error:
                errors.append(name.upper() + ":" + error)
    result = {
        **flow,
        "broker": broker,
        "version": VERSION,
        "analytics": analytics,
        "errors": errors,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "trade_blocking": False,
        "order_execution": False,
    }
    with _lock:
        _cache[int(user_id)] = {"mono": current, "value": dict(result)}
    return result


def _overlay_native_analytics(broker_module, result, flow):
    option = dict(result.get("option_intelligence") or {})
    analytics = dict(flow.get("analytics") or {})
    rows = [dict(row) for row in (option.get("rows") or [])]
    by_strike = {
        round(_f(row.get("strike")), 4): row
        for row in rows
    }
    oi = analytics.get("oi") if isinstance(analytics.get("oi"), Mapping) else {}
    for item in oi.get("call_put_oi_data_list") or []:
        row = by_strike.get(round(_f(item.get("strike_price")), 4))
        if not row:
            continue
        ce = dict(row.get("ce") or {})
        pe = dict(row.get("pe") or {})
        ce["oi"] = _f(item.get("call_oi"), ce.get("oi"))
        pe["oi"] = _f(item.get("put_oi"), pe.get("oi"))
        row["ce"], row["pe"] = ce, pe
    change = (
        analytics.get("change_oi")
        if isinstance(analytics.get("change_oi"), Mapping)
        else {}
    )
    for item in change.get("call_put_oi_data_list") or []:
        row = by_strike.get(round(_f(item.get("strike_price")), 4))
        if not row:
            continue
        for key, field in (("ce", "call_change_oi"), ("pe", "put_change_oi")):
            contract = dict(row.get(key) or {})
            current_oi = _f(contract.get("oi"))
            oi_change = _f(item.get(field))
            if current_oi > 0:
                contract["prev_oi"] = max(0.0, current_oi - oi_change)
            row[key] = contract
    if rows:
        summary = broker_module.summarize_chain(
            {
                "broker": result.get("broker"),
                "underlying": option.get("underlying"),
                "spot": option.get("spot"),
                "expiry": option.get("expiry"),
                "rows": list(by_strike.values()),
                "native_option_chain": True,
            },
            {},
        )
    else:
        summary = option
    pcr = analytics.get("pcr") if isinstance(analytics.get("pcr"), Mapping) else {}
    if _f(pcr.get("pcr")) > 0:
        summary["pcr"] = round(_f(pcr.get("pcr")), 4)
    max_pain = (
        analytics.get("max_pain")
        if isinstance(analytics.get("max_pain"), Mapping)
        else {}
    )
    native_max_pain = _f(
        max_pain.get("max_pain") or max_pain.get("max_pain_strike")
    )
    if native_max_pain > 0:
        summary["max_pain"] = native_max_pain
        spot = _f(summary.get("spot"))
        summary["max_pain_distance_percent"] = (
            round((spot - native_max_pain) / spot * 100, 4) if spot else 0
        )
    flow_direction = str(flow.get("direction") or "NEUTRAL")
    flow_confidence = int(_f(flow.get("confidence")))
    current_direction = str(summary.get("option_direction") or "NO_TRADE")
    if flow.get("available") and flow_direction in {"CE", "PE"}:
        summary.setdefault("reasons", []).append(
            "INSTITUTIONAL_FLOW_" + flow_direction
        )
        if current_direction == flow_direction:
            summary["option_confidence"] = min(
                94,
                int(_f(summary.get("option_confidence")))
                + max(3, (flow_confidence - 45) // 5),
            )
        elif current_direction in {"CE", "PE"}:
            summary["risk_score"] = min(
                100,
                int(_f(summary.get("risk_score"))) + 12,
            )
            summary["reasons"].append("OPTION_INSTITUTIONAL_CONFLICT")
            if flow_confidence >= 70:
                summary["option_direction"] = "NO_TRADE"
        elif flow_confidence >= 60:
            summary["option_direction"] = flow_direction
            summary["option_confidence"] = min(68, flow_confidence)
    summary["institutional_flow"] = {
        key: value for key, value in flow.items() if key != "analytics"
    }
    summary["native_analytics_available"] = sorted(analytics)
    result["option_intelligence"] = summary
    result["institutional_flow"] = summary["institutional_flow"]
    return result


def install():
    global _installed
    if _installed:
        return True
    from bot import broker_intelligence as broker_module

    original = broker_module.get_broker_intelligence

    def wrapped(user_id, market_snapshot, previous_oi=None, wings=None):
        kwargs = {}
        if wings is not None:
            kwargs["wings"] = wings
        result = original(user_id, market_snapshot, previous_oi, **kwargs)
        option = dict(result.get("option_intelligence") or {})
        try:
            flow = _fetch(user_id, broker_module, option)
            result = _overlay_native_analytics(broker_module, result, flow)
        except Exception as exc:
            result["institutional_flow"] = {
                "available": False,
                "source": "SAFE_NEUTRAL_FALLBACK",
                "direction": "NEUTRAL",
                "confidence": 0,
                "reason": f"{type(exc).__name__}:{str(exc)[:160]}",
                "trade_blocking": False,
                "order_execution": False,
            }
        return result

    broker_module.get_broker_intelligence = wrapped
    advanced = sys.modules.get("bot.advanced_intelligence_v2")
    if advanced is not None:
        advanced.get_broker_intelligence = wrapped
    _installed = True
    return True
