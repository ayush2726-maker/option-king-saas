"""Nearest-expiry resolver and hard validation for Upstox option entries.

The Instrument Search API can rank a monthly contract ahead of a nearer weekly
contract. Resolve from the official option-contract list, sort real expiry dates,
and use an underlying-specific distance guard. BANKNIFTY has monthly expiries
only, so it must not use the weekly 8-day limit.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

import requests

VERSION = "OKAI-UPSTOX-NEAREST-EXPIRY-V2"

UNDERLYING_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}
NORMAL_EXPIRY_REQUESTS = {
    "",
    "current_week",
    "current_month",
    "nearest",
    "nearest_expiry",
}
MAX_NORMAL_DTE_BY_UNDERLYING = {
    "NIFTY": 8,
    "SENSEX": 8,
    "BANKNIFTY": 40,
}


def _today_ist() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def _parse_expiry(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    for candidate in (raw[:10], raw):
        for fmt in (
            "%Y-%m-%d",
            "%d-%m-%Y",
            "%d%b%Y",
            "%d-%b-%Y",
            "%d %b %y",
            "%d %b %Y",
        ):
            try:
                return datetime.strptime(candidate.upper(), fmt).date()
            except (TypeError, ValueError):
                continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _normal_underlying(value: Any) -> str:
    raw = str(value or "").upper().replace(" ", "").replace("-", "")
    if raw in {"NIFTY50", "NIFTY"}:
        return "NIFTY"
    if raw in {"NIFTYBANK", "BANKNIFTY"}:
        return "BANKNIFTY"
    if "SENSEX" in raw:
        return "SENSEX"
    return raw


def _is_normal_request(expiry: Any) -> bool:
    return str(expiry or "current_week").strip().lower() in NORMAL_EXPIRY_REQUESTS


def _max_normal_dte(underlying: Any) -> int:
    return MAX_NORMAL_DTE_BY_UNDERLYING.get(_normal_underlying(underlying), 8)


def _candidate_contracts(
    rows: Iterable[dict],
    underlying: str,
    strike: float,
    option_type: str,
    today: date,
    requested_day: Optional[date] = None,
) -> list[Tuple[date, float, int, dict]]:
    expected_underlying = _normal_underlying(underlying)
    expected_type = str(option_type or "").upper().strip()
    output: list[Tuple[date, float, int, dict]] = []

    for raw in rows or []:
        row = dict(raw or {})
        row_type = str(
            row.get("instrument_type")
            or row.get("option_type")
            or ""
        ).upper()
        if row_type != expected_type:
            continue

        row_underlying = _normal_underlying(
            row.get("underlying_symbol")
            or row.get("name")
            or underlying
        )
        if row_underlying != expected_underlying:
            continue

        expiry_day = _parse_expiry(row.get("expiry"))
        if expiry_day is None or expiry_day < today:
            continue
        if requested_day is not None and expiry_day != requested_day:
            continue

        try:
            row_strike = float(row.get("strike_price") or row.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if row_strike <= 0:
            continue

        weekly_rank = 0 if bool(row.get("weekly")) else 1
        output.append(
            (
                expiry_day,
                abs(row_strike - float(strike)),
                weekly_rank,
                row,
            )
        )
    return output


def _pick_contract(
    rows: Iterable[dict],
    underlying: str,
    strike: float,
    option_type: str,
    today: date,
    requested_day: Optional[date] = None,
) -> Optional[Tuple[date, dict]]:
    candidates = _candidate_contracts(
        rows,
        underlying,
        strike,
        option_type,
        today,
        requested_day,
    )
    if not candidates:
        return None
    expiry_day, _, _, row = min(
        candidates,
        key=lambda item: (item[0], item[1], item[2]),
    )
    return expiry_day, row


def _result_from_row(
    row: dict,
    expiry_day: date,
    today: date,
    source: str,
    underlying: str,
) -> dict:
    name = _normal_underlying(underlying)
    return {
        "success": True,
        "symbol": str(row.get("trading_symbol") or row.get("tradingsymbol") or ""),
        "token": str(row.get("instrument_key") or row.get("token") or ""),
        "exchange": str(
            row.get("segment")
            or row.get("exchange")
            or ("BSE_FO" if name == "SENSEX" else "NSE_FO")
        ),
        "expiry": expiry_day.isoformat(),
        "expiry_dte": (expiry_day - today).days,
        "expiry_source": source,
        "strike": float(row.get("strike_price") or row.get("strike") or 0),
        "lot_size": int(
            row.get("lot_size")
            or row.get("minimum_lot")
            or row.get("minimumlot")
            or 0
        ),
        "weekly": bool(row.get("weekly")),
        "underlying": name,
        "resolver_version": VERSION,
    }


def _validate_result(
    result: Any,
    underlying: Any,
    expiry: Any,
    today: date,
    source: str,
) -> dict:
    if not isinstance(result, dict) or not result.get("success"):
        return result if isinstance(result, dict) else {
            "success": False,
            "message": "UPSTOX_OPTION_NOT_FOUND",
            "resolver_version": VERSION,
        }

    name = _normal_underlying(underlying)
    expiry_day = _parse_expiry(result.get("expiry"))
    if expiry_day is None:
        return {
            "success": False,
            "message": "UPSTOX_EXPIRY_INVALID",
            "resolver_version": VERSION,
        }

    dte = (expiry_day - today).days
    if dte < 0:
        return {
            "success": False,
            "message": "UPSTOX_EXPIRY_EXPIRED",
            "expiry": expiry_day.isoformat(),
            "expiry_dte": dte,
            "resolver_version": VERSION,
        }

    requested_day = None if _is_normal_request(expiry) else _parse_expiry(expiry)
    if requested_day is not None and expiry_day != requested_day:
        return {
            "success": False,
            "message": "EXPIRY_MISMATCH",
            "requested_expiry": str(expiry),
            "expected_expiry": requested_day.isoformat(),
            "expiry": expiry_day.isoformat(),
            "expiry_dte": dte,
            "resolver_version": VERSION,
        }

    max_dte = _max_normal_dte(name)
    if _is_normal_request(expiry) and dte > max_dte:
        return {
            "success": False,
            "message": "EXPIRY_TOO_FAR",
            "requested_expiry": str(expiry or "current_week"),
            "expiry": expiry_day.isoformat(),
            "expiry_dte": dte,
            "max_expiry_dte": max_dte,
            "resolver_version": VERSION,
        }

    output = dict(result)
    output["success"] = True
    output["expiry"] = expiry_day.isoformat()
    output["expiry_dte"] = dte
    output["max_expiry_dte"] = max_dte
    output["expiry_source"] = source
    output["resolver_version"] = VERSION
    return output


def install(upstox_broker_class) -> bool:
    if getattr(upstox_broker_class, "_okai_nearest_expiry_v2", False):
        return True

    original_search_option = upstox_broker_class.search_option

    def search_option_nearest(self, underlying, expiry, strike, option_type):
        name = _normal_underlying(underlying)
        requested_expiry = str(expiry or "current_week").strip()
        today = _today_ist()
        underlying_key = UNDERLYING_KEYS.get(name)
        requested_day = (
            None
            if _is_normal_request(requested_expiry)
            else _parse_expiry(requested_expiry)
        )

        if not underlying_key:
            fallback = original_search_option(
                self,
                underlying,
                expiry,
                strike,
                option_type,
            )
            return _validate_result(
                fallback,
                name,
                requested_expiry,
                today,
                "INSTRUMENT_SEARCH_VALIDATED_FALLBACK",
            )

        errors = []
        request_variants = [requested_expiry]
        if _is_normal_request(requested_expiry):
            request_variants.append(None)

        for expiry_filter in request_variants:
            try:
                params: Dict[str, Any] = {"instrument_key": underlying_key}
                if expiry_filter:
                    params["expiry_date"] = expiry_filter

                response = requests.get(
                    f"{self.BASE_URL}/option/contract",
                    params=params,
                    headers=self._h(),
                    timeout=20,
                )
                payload = response.json()
                if response.status_code != 200 or payload.get("status") != "success":
                    errors.append(str(payload.get("errors") or payload)[:180])
                    continue

                picked = _pick_contract(
                    payload.get("data") or [],
                    name,
                    float(strike),
                    option_type,
                    today,
                    requested_day,
                )
                if picked is None:
                    errors.append("UPSTOX_OPTION_CONTRACTS_EMPTY")
                    continue

                expiry_day, best = picked
                candidate = _result_from_row(
                    best,
                    expiry_day,
                    today,
                    (
                        "OPTION_CONTRACT_FILTERED"
                        if expiry_filter
                        else "OPTION_CONTRACT_ALL_LIVE_RECOVERY"
                    ),
                    name,
                )
                validated = _validate_result(
                    candidate,
                    name,
                    requested_expiry,
                    today,
                    candidate["expiry_source"],
                )
                if validated.get("success"):
                    return validated
                errors.append(
                    f"{validated.get('message')}:{validated.get('expiry')}:{validated.get('expiry_dte')}"
                )
            except Exception as exc:
                errors.append(str(exc)[:180])

        fallback = original_search_option(
            self,
            underlying,
            expiry,
            strike,
            option_type,
        )
        validated_fallback = _validate_result(
            fallback,
            name,
            requested_expiry,
            today,
            "INSTRUMENT_SEARCH_VALIDATED_FALLBACK",
        )
        if validated_fallback.get("success"):
            return validated_fallback

        return {
            "success": False,
            "message": str(
                validated_fallback.get("message")
                or "EXPIRY_RESOLUTION_FAILED"
            ),
            "reason": "NEAREST_VALID_EXPIRY_NOT_AVAILABLE",
            "underlying": name,
            "requested_expiry": requested_expiry or "current_week",
            "max_expiry_dte": _max_normal_dte(name),
            "errors": errors[-3:],
            "resolver_version": VERSION,
        }

    upstox_broker_class.search_option = search_option_nearest
    upstox_broker_class._okai_nearest_expiry_v1 = True
    upstox_broker_class._okai_nearest_expiry_v2 = True
    upstox_broker_class._okai_nearest_expiry_version = VERSION
    return True
