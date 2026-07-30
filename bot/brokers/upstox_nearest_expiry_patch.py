"""Hard guard and nearest-expiry resolver for Upstox option entries.

The old Instrument Search route can return a monthly contract even when
``current_week`` is requested. This patch resolves tradable contracts from the
official ``/option/contract`` endpoint, sorts by real dates, and refuses a far
expiry for normal intraday entries.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

import requests

VERSION = "OKAI-UPSTOX-NEAREST-EXPIRY-V1"
MAX_NORMAL_DTE = 8

UNDERLYING_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}
NORMAL_EXPIRY_REQUESTS = {"", "current_week"}


def _today_ist() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def _parse_expiry(value: Any) -> Optional[date]:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidates = (raw[:10], raw)
    formats = ("%Y-%m-%d", "%d-%m-%Y", "%d %b %y", "%d %b %Y")
    for candidate in candidates:
        for fmt in formats:
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


def _candidate_contracts(
    rows: Iterable[dict],
    underlying: str,
    strike: float,
    option_type: str,
    today: date,
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

        try:
            row_strike = float(row.get("strike_price") or row.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if row_strike <= 0:
            continue

        weekly_rank = 0 if bool(row.get("weekly")) else 1
        output.append((expiry_day, abs(row_strike - float(strike)), weekly_rank, row))

    return output


def _pick_contract(
    rows: Iterable[dict],
    underlying: str,
    strike: float,
    option_type: str,
    today: date,
) -> Optional[Tuple[date, dict]]:
    candidates = _candidate_contracts(
        rows,
        underlying,
        strike,
        option_type,
        today,
    )
    if not candidates:
        return None
    expiry_day, _, _, row = min(
        candidates,
        key=lambda item: (item[0], item[1], item[2]),
    )
    return expiry_day, row


def _result_from_row(row: dict, expiry_day: date, today: date, source: str) -> dict:
    underlying = _normal_underlying(
        row.get("underlying_symbol") or row.get("name")
    )
    return {
        "success": True,
        "symbol": str(row.get("trading_symbol") or row.get("tradingsymbol") or ""),
        "token": str(row.get("instrument_key") or row.get("token") or ""),
        "exchange": str(
            row.get("segment")
            or row.get("exchange")
            or ("BSE_FO" if underlying == "SENSEX" else "NSE_FO")
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
        "resolver_version": VERSION,
    }


def _validated_fallback(
    original,
    obj,
    underlying,
    expiry,
    strike,
    option_type,
    today: date,
) -> dict:
    result = original(obj, underlying, expiry, strike, option_type)
    if not isinstance(result, dict) or not result.get("success"):
        return result if isinstance(result, dict) else {
            "success": False,
            "message": "UPSTOX_OPTION_NOT_FOUND",
        }

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
    if _is_normal_request(expiry) and dte > MAX_NORMAL_DTE:
        return {
            "success": False,
            "message": "EXPIRY_TOO_FAR",
            "requested_expiry": str(expiry or "current_week"),
            "expiry": expiry_day.isoformat(),
            "expiry_dte": dte,
            "max_expiry_dte": MAX_NORMAL_DTE,
            "resolver_version": VERSION,
        }

    result = dict(result)
    result["expiry"] = expiry_day.isoformat()
    result["expiry_dte"] = dte
    result["expiry_source"] = "INSTRUMENT_SEARCH_VALIDATED_FALLBACK"
    result["resolver_version"] = VERSION
    return result


def install(upstox_broker_class) -> bool:
    if getattr(upstox_broker_class, "_okai_nearest_expiry_v1", False):
        return True

    original_search_option = upstox_broker_class.search_option

    def search_option_nearest(self, underlying, expiry, strike, option_type):
        underlying_name = _normal_underlying(underlying)
        requested_expiry = str(expiry or "current_week").strip()
        today = _today_ist()
        underlying_key = UNDERLYING_KEYS.get(underlying_name)

        if not underlying_key:
            return _validated_fallback(
                original_search_option,
                self,
                underlying,
                expiry,
                strike,
                option_type,
                today,
            )

        errors = []
        request_variants = [requested_expiry]
        if _is_normal_request(requested_expiry):
            # A no-expiry request returns all live contracts and is the recovery
            # path when Upstox's relative keyword resolves to a monthly contract.
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
                    underlying_name,
                    float(strike),
                    option_type,
                    today,
                )
                if picked is None:
                    errors.append("UPSTOX_OPTION_CONTRACTS_EMPTY")
                    continue

                expiry_day, best = picked
                dte = (expiry_day - today).days
                if _is_normal_request(requested_expiry) and dte > MAX_NORMAL_DTE:
                    errors.append(f"EXPIRY_TOO_FAR:{expiry_day.isoformat()}:{dte}")
                    continue

                return _result_from_row(
                    best,
                    expiry_day,
                    today,
                    "OPTION_CONTRACT_CURRENT_WEEK"
                    if expiry_filter
                    else "OPTION_CONTRACT_ALL_LIVE_RECOVERY",
                )
            except Exception as exc:
                errors.append(str(exc)[:180])

        fallback = _validated_fallback(
            original_search_option,
            self,
            underlying,
            expiry,
            strike,
            option_type,
            today,
        )
        if isinstance(fallback, dict) and fallback.get("success"):
            return fallback

        if _is_normal_request(requested_expiry):
            return {
                "success": False,
                "message": fallback.get("message") if isinstance(fallback, dict) else "EXPIRY_RESOLUTION_FAILED",
                "reason": "NEAREST_WEEKLY_EXPIRY_NOT_AVAILABLE",
                "requested_expiry": requested_expiry or "current_week",
                "max_expiry_dte": MAX_NORMAL_DTE,
                "errors": errors[-3:],
                "resolver_version": VERSION,
            }
        return fallback

    upstox_broker_class.search_option = search_option_nearest
    upstox_broker_class._okai_nearest_expiry_v1 = True
    upstox_broker_class._okai_nearest_expiry_version = VERSION
    return True
