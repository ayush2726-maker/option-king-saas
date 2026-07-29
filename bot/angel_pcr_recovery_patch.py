"""Angel One native PCR recovery for broker option intelligence.

The normal option-intelligence path derives PCR from the assembled option-chain OI.
Angel sometimes returns live premium/depth while omitting OI in the FULL quote window,
which leaves PCR as None and the mobile app shows "--".  This patch keeps the
existing chain-derived PCR whenever it is available, and only falls back to Angel's
native put-call-ratio endpoint when the chain PCR is missing.

Display/monitoring only: no entry, exit, risk, quantity or order-execution logic is
changed here.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, Iterable, Mapping, Optional


PCR_KEYS = (
    "pcr",
    "PCR",
    "putCallRatio",
    "put_call_ratio",
    "putCallOIRatio",
    "put_call_oi_ratio",
    "putCallOpenInterestRatio",
    "put_call_open_interest_ratio",
    "ratio",
)

TEXT_KEYS = (
    "name",
    "symbol",
    "tradingSymbol",
    "tradingsymbol",
    "underlying",
    "underlyingSymbol",
    "underlying_symbol",
    "index",
    "indexName",
    "index_name",
)


def _f(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        number = float(str(value).replace(",", "").strip())
        if number == number and number > 0:
            return number
    except (TypeError, ValueError):
        pass
    return default


def _compact(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _aliases(underlying: Any) -> set:
    name = _compact(underlying or "NIFTY")
    if name in {"BANKNIFTY", "NIFTYBANK", "NIFTYBANKINDEX"}:
        return {"BANKNIFTY", "NIFTYBANK", "NIFTYBANKINDEX"}
    if name in {"SENSEX", "BSENSEX", "BSESENSEX"}:
        return {"SENSEX", "BSENSEX", "BSESENSEX"}
    return {"NIFTY", "NIFTY50", "NIFTYFIFTY", "NIFTYINDEX"}


def _iter_dicts(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_dicts(child)


def _matches_underlying(row: Mapping[str, Any], underlying: Any) -> bool:
    aliases = _aliases(underlying)
    text_values = [_compact(row.get(key)) for key in TEXT_KEYS if row.get(key) is not None]
    if any(value in aliases for value in text_values):
        return True
    blob = _compact(" ".join(str(row.get(key) or "") for key in TEXT_KEYS))
    if not blob:
        return False
    if "NIFTY" in aliases and ("BANKNIFTY" in blob or "NIFTYBANK" in blob):
        return False
    return any(alias and alias in blob for alias in aliases)


def _extract_pcr_value(payload: Any, underlying: Any) -> Optional[Dict[str, Any]]:
    candidates = []
    for row in _iter_dicts(payload):
        value = None
        key_used = None
        for key in PCR_KEYS:
            if key in row:
                value = _f(row.get(key), None)
                key_used = key
                break
        if value is None:
            continue
        match = _matches_underlying(row, underlying)
        candidates.append({"value": value, "matched": match, "key": key_used, "row": dict(row)})

    matched = [item for item in candidates if item["matched"]]
    if matched:
        best = matched[0]
    elif len(candidates) == 1:
        best = candidates[0]
    else:
        return None

    return {
        "value": float(best["value"]),
        "source": "ANGEL_NATIVE_PUT_CALL_RATIO",
        "field": best.get("key"),
        "matched_underlying": bool(best.get("matched")),
    }


def _call_native_angel_pcr(obj: Any, underlying: Any) -> Optional[Dict[str, Any]]:
    method_names = (
        "putCallRatio",
        "getPutCallRatio",
        "put_call_ratio",
        "getPutCallRatioData",
        "pcr",
    )
    attempts = (
        (),
        ({"name": str(underlying or "").upper()},),
        ({"symbol": str(underlying or "").upper()},),
        (str(underlying or "").upper(),),
    )

    last_error = None
    for method_name in method_names:
        method = getattr(obj, method_name, None)
        if not callable(method):
            continue
        for args in attempts:
            try:
                response = method(*args)
            except TypeError as exc:
                last_error = exc
                continue
            except Exception as exc:  # broker/session can reject some call shapes
                last_error = exc
                continue
            extracted = _extract_pcr_value(response, underlying)
            if extracted and _f(extracted.get("value"), None) is not None:
                extracted["method"] = method_name
                return extracted
    if last_error:
        return {"error": f"{type(last_error).__name__}:{str(last_error)[:180]}"}
    return None


def _inject_pcr(result: Dict[str, Any], native: Optional[Mapping[str, Any]] = None, error: Optional[str] = None) -> Dict[str, Any]:
    option = dict(result.get("option_intelligence") or {})
    if not option:
        return result

    existing = _f(option.get("pcr"), None)
    if existing is not None:
        option.setdefault("pcr_source", "CHAIN_OI")
        result["option_intelligence"] = option
        return result

    if native and _f(native.get("value"), None) is not None:
        option["pcr"] = round(float(native["value"]), 4)
        option["pcr_source"] = str(native.get("source") or "ANGEL_NATIVE_PUT_CALL_RATIO")
        option["pcr_recovered"] = True
        option["pcr_field"] = native.get("field")
        option["pcr_method"] = native.get("method")
        reasons = list(option.get("reasons") or [])
        reasons.append("PCR_RECOVERED_FROM_ANGEL_NATIVE")
        option["reasons"] = list(dict.fromkeys(reasons))[:12]
    else:
        option.setdefault("pcr_source", "UNAVAILABLE")
        if error:
            option["pcr_error"] = str(error)[:220]
        elif native and native.get("error"):
            option["pcr_error"] = str(native.get("error"))[:220]

    result["option_intelligence"] = option
    return result


def apply_angel_pcr_recovery_patch() -> None:
    from bot import broker_intelligence as broker

    if getattr(broker, "_okai_angel_pcr_recovery_v1", False):
        return

    original_get_broker_intelligence = broker.get_broker_intelligence

    def get_broker_intelligence(user_id, market_snapshot, previous_oi=None, wings=broker.DEFAULT_WINGS):
        result = dict(original_get_broker_intelligence(user_id, market_snapshot, previous_oi, wings) or {})
        if str(result.get("broker") or "").lower() != "angelone":
            return _inject_pcr(result)

        option = dict(result.get("option_intelligence") or {})
        if _f(option.get("pcr"), None) is not None:
            return _inject_pcr(result)

        native = None
        error = None
        try:
            broker_name, creds = broker._get_active_broker(int(user_id))
            if str(broker_name or "").lower() == "angelone" and creds:
                obj = broker._get_ltp_session(user_id, creds)
                native = _call_native_angel_pcr(
                    obj,
                    result.get("underlying")
                    or market_snapshot.get("symbol")
                    or market_snapshot.get("underlying")
                    or "NIFTY",
                )
        except Exception as exc:
            error = f"{type(exc).__name__}:{str(exc)[:180]}"

        return _inject_pcr(result, native=native, error=error)

    broker.get_broker_intelligence = get_broker_intelligence

    advanced = sys.modules.get("bot.advanced_intelligence_v2")
    if advanced is not None and getattr(advanced, "get_broker_intelligence", None) is original_get_broker_intelligence:
        advanced.get_broker_intelligence = get_broker_intelligence

    broker._okai_angel_pcr_recovery_v1 = True
