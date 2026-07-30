"""Angel One native/derived PCR recovery for broker option intelligence.

The normal option-intelligence path derives PCR from the assembled option-chain OI.
Angel can return live premium/depth while OI arrives under different field names or
while the SmartAPI wrapper does not expose putCallRatio as a first-class method.
This patch keeps chain PCR whenever it exists, recovers OI aliases before chain
summary, and falls back to Angel's native put-call-ratio route when chain PCR is
still missing.

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

# Angel FULL quote has changed field casing across wrappers/responses.  Keep this
# permissive and exact-normalized; do not treat OI change fields as current OI.
OI_KEYS = {
    "oi",
    "openinterest",
    "opninterest",
    "openint",
    "openinterestqty",
    "openinterestquantity",
    "openinterestvalue",
}
PREV_OI_KEYS = {
    "prevoi",
    "previousoi",
    "previousopeninterest",
    "prevopeninterest",
    "prevopninterest",
}


def _norm_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


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


def _extract_number_by_keys(payload: Any, keys: set) -> Optional[float]:
    for row in _iter_dicts(payload):
        for key, value in row.items():
            normalized = _norm_key(key)
            if normalized in keys:
                number = _f(value, None)
                if number is not None:
                    return float(number)
    return None


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


def _call_obj_route(obj: Any, route_key: str, params: Optional[dict] = None) -> Any:
    # SmartApi-python commonly exposes _postRequest(route, params).  Some older
    # versions accept only route.  Try both without making startup depend on it.
    method = getattr(obj, "_postRequest", None)
    if not callable(method):
        raise AttributeError("SmartConnect._postRequest missing")
    try:
        return method(route_key, params or {})
    except TypeError:
        return method(route_key)


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
    saw_callable = False
    for method_name in method_names:
        method = getattr(obj, method_name, None)
        if not callable(method):
            continue
        saw_callable = True
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
            last_error = RuntimeError("PCR_VALUE_NOT_FOUND_IN_METHOD_RESPONSE")

    # Fallback for SmartApi wrappers where the route exists but the public method
    # is absent or stale.
    route_keys = (
        "api.market.putcallratio",
        "api.market.putCallRatio",
        "api.market.put_call_ratio",
    )
    for route_key in route_keys:
        try:
            response = _call_obj_route(obj, route_key, {})
        except Exception as exc:
            last_error = exc
            continue
        extracted = _extract_pcr_value(response, underlying)
        if extracted and _f(extracted.get("value"), None) is not None:
            extracted["method"] = "_postRequest"
            extracted["route"] = route_key
            return extracted
        last_error = RuntimeError("PCR_VALUE_NOT_FOUND_IN_ROUTE_RESPONSE")

    if last_error:
        return {"error": f"{type(last_error).__name__}:{str(last_error)[:180]}"}
    if not saw_callable:
        return {"error": "ANGEL_NATIVE_PCR_METHOD_NOT_AVAILABLE"}
    return {"error": "ANGEL_NATIVE_PCR_UNAVAILABLE"}


def _chain_pcr_from_option_rows(option: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    call_oi = put_oi = 0.0
    for row in option.get("rows") or []:
        row = dict(row or {})
        call_oi += _f((row.get("ce") or {}).get("oi"), 0.0) or 0.0
        put_oi += _f((row.get("pe") or {}).get("oi"), 0.0) or 0.0
    if call_oi > 0:
        return {
            "value": put_oi / call_oi,
            "source": "CHAIN_OI_RECOVERED",
            "total_call_oi": call_oi,
            "total_put_oi": put_oi,
        }
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

    recovered_chain = _chain_pcr_from_option_rows(option)
    if recovered_chain and _f(recovered_chain.get("value"), None) is not None:
        option["pcr"] = round(float(recovered_chain["value"]), 4)
        option["pcr_source"] = recovered_chain["source"]
        option["pcr_recovered"] = True
        option["total_call_oi"] = round(float(recovered_chain.get("total_call_oi") or 0), 2)
        option["total_put_oi"] = round(float(recovered_chain.get("total_put_oi") or 0), 2)
        reasons = list(option.get("reasons") or [])
        reasons.append("PCR_RECOVERED_FROM_CHAIN_OI")
        option["reasons"] = list(dict.fromkeys(reasons))[:12]
    elif native and _f(native.get("value"), None) is not None:
        option["pcr"] = round(float(native["value"]), 4)
        option["pcr_source"] = str(native.get("source") or "ANGEL_NATIVE_PUT_CALL_RATIO")
        option["pcr_recovered"] = True
        option["pcr_field"] = native.get("field")
        option["pcr_method"] = native.get("method")
        if native.get("route"):
            option["pcr_route"] = native.get("route")
        reasons = list(option.get("reasons") or [])
        reasons.append("PCR_RECOVERED_FROM_ANGEL_NATIVE")
        option["reasons"] = list(dict.fromkeys(reasons))[:12]
    else:
        option.setdefault("pcr_source", "UNAVAILABLE")
        call_oi = _f(option.get("total_call_oi"), 0.0) or 0.0
        put_oi = _f(option.get("total_put_oi"), 0.0) or 0.0
        if error:
            option["pcr_error"] = str(error)[:220]
        elif native and native.get("error"):
            option["pcr_error"] = str(native.get("error"))[:220]
        else:
            option["pcr_error"] = f"CALL_OI_ZERO_OR_NATIVE_PCR_EMPTY call_oi={call_oi} put_oi={put_oi}"
        reasons = list(option.get("reasons") or [])
        reasons.append("PCR_UNAVAILABLE_DIAGNOSTIC_ATTACHED")
        option["reasons"] = list(dict.fromkeys(reasons))[:12]

    result["option_intelligence"] = option
    return result


def recover_pcr_for_result(user_id: int, market_snapshot: Mapping[str, Any], result: Mapping[str, Any]) -> Dict[str, Any]:
    """Recover/diagnose PCR on an already-built broker-intelligence payload."""
    from bot import broker_intelligence as broker

    output = dict(result or {})
    option = dict(output.get("option_intelligence") or {})
    if not option:
        return output
    if _f(option.get("pcr"), None) is not None:
        return _inject_pcr(output)

    native = None
    error = None
    if str(output.get("broker") or "").lower() == "angelone":
        try:
            broker_name, creds = broker._get_active_broker(int(user_id))
            if str(broker_name or "").lower() == "angelone" and creds:
                obj = broker._get_ltp_session(user_id, creds)
                native = _call_native_angel_pcr(
                    obj,
                    output.get("underlying")
                    or market_snapshot.get("symbol")
                    or market_snapshot.get("underlying")
                    or "NIFTY",
                )
        except Exception as exc:
            error = f"{type(exc).__name__}:{str(exc)[:180]}"
    return _inject_pcr(output, native=native, error=error)


def _install_oi_alias_patch(broker: Any) -> None:
    if getattr(broker, "_okai_angel_oi_alias_v1", False):
        return

    original_normal_option = broker._normal_option
    original_angel_market_data = broker._angel_market_data

    def normal_option_with_oi_alias(*args, **kwargs):
        market = dict(kwargs.get("market") or {})
        option = dict(original_normal_option(*args, **kwargs) or {})
        if _f(option.get("oi"), None) is None:
            oi = _extract_number_by_keys(market, OI_KEYS)
            if oi is not None:
                option["oi"] = round(float(oi), 2)
                option["oi_source"] = "ANGEL_FULL_QUOTE_ALIAS"
        if _f(option.get("prev_oi"), None) is None:
            prev = _extract_number_by_keys(market, PREV_OI_KEYS)
            if prev is not None:
                option["prev_oi"] = round(float(prev), 2)
                option["prev_oi_source"] = "ANGEL_FULL_QUOTE_ALIAS"
        return option

    def angel_market_data_with_oi_alias(obj, contracts):
        result = dict(original_angel_market_data(obj, contracts) or {})
        for token, row in list(result.items()):
            if not isinstance(row, dict):
                continue
            oi = _extract_number_by_keys(row, OI_KEYS)
            prev = _extract_number_by_keys(row, PREV_OI_KEYS)
            if oi is not None and _f(row.get("oi"), None) is None:
                row["oi"] = oi
            if prev is not None and _f(row.get("prev_oi"), None) is None:
                row["prev_oi"] = prev
            result[token] = row
        return result

    broker._normal_option = normal_option_with_oi_alias
    broker._angel_market_data = angel_market_data_with_oi_alias
    try:
        broker.BROKER_CAPABILITIES["angelone"]["pcr"] = "DERIVED_FROM_CHAIN_WITH_NATIVE_FALLBACK"
    except Exception:
        pass
    broker._okai_angel_oi_alias_v1 = True


def apply_angel_pcr_recovery_patch() -> None:
    from bot import broker_intelligence as broker

    _install_oi_alias_patch(broker)

    if getattr(broker, "_okai_angel_pcr_recovery_v2", False):
        return

    original_get_broker_intelligence = broker.get_broker_intelligence

    def get_broker_intelligence(user_id, market_snapshot, previous_oi=None, wings=broker.DEFAULT_WINGS):
        result = dict(original_get_broker_intelligence(user_id, market_snapshot, previous_oi, wings) or {})
        return recover_pcr_for_result(user_id, market_snapshot, result)

    broker.get_broker_intelligence = get_broker_intelligence

    advanced = sys.modules.get("bot.advanced_intelligence_v2")
    if advanced is not None:
        advanced.get_broker_intelligence = get_broker_intelligence

    broker._okai_angel_pcr_recovery_v1 = True
    broker._okai_angel_pcr_recovery_v2 = True
