"""Broker-neutral nearest-expiry selection and hard validation.

AUTO paper/live entry requests use relative expiry names such as ``current_week``.
The meaning must be "nearest tradable expiry", not "whatever the broker search
ranks first". NIFTY and SENSEX have weekly expiries, while BANKNIFTY has monthly
expiries only, so the allowed distance is underlying-specific.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

VERSION = "OKAI-BROKER-EXPIRY-GUARD-V1"

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
    # BANKNIFTY weekly options are discontinued. Its nearest valid monthly
    # contract can be roughly a month away immediately after expiry.
    "BANKNIFTY": 40,
}


def _today_ist() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def _normal_underlying(value: Any) -> str:
    raw = str(value or "").upper().replace(" ", "").replace("-", "")
    if raw in {"NIFTY", "NIFTY50"}:
        return "NIFTY"
    if raw in {"BANKNIFTY", "NIFTYBANK"}:
        return "BANKNIFTY"
    if "SENSEX" in raw:
        return "SENSEX"
    return raw


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
            "%d %b %Y",
            "%d %b %y",
        ):
            try:
                return datetime.strptime(candidate.upper(), fmt).date()
            except (TypeError, ValueError):
                continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _is_normal_request(expiry: Any) -> bool:
    return str(expiry or "current_week").strip().lower() in NORMAL_EXPIRY_REQUESTS


def _max_normal_dte(underlying: Any) -> int:
    return MAX_NORMAL_DTE_BY_UNDERLYING.get(_normal_underlying(underlying), 8)


def _failure(message: str, underlying: Any, requested_expiry: Any, **extra) -> dict:
    return {
        "success": False,
        "message": str(message),
        "underlying": _normal_underlying(underlying),
        "requested_expiry": str(requested_expiry or "current_week"),
        "resolver_version": VERSION,
        **extra,
    }


def _validate_result(
    result: Any,
    underlying: Any,
    requested_expiry: Any,
    *,
    today: Optional[date] = None,
) -> dict:
    if not isinstance(result, dict):
        return _failure(
            "OPTION_CONTRACT_NOT_RESOLVED",
            underlying,
            requested_expiry,
        )
    if not result.get("success"):
        output = dict(result)
        output.setdefault("resolver_version", VERSION)
        return output

    today = today or _today_ist()
    expiry_day = _parse_expiry(result.get("expiry"))
    if expiry_day is None:
        return _failure(
            "EXPIRY_MISSING_OR_INVALID",
            underlying,
            requested_expiry,
        )

    dte = (expiry_day - today).days
    if dte < 0:
        return _failure(
            "EXPIRY_EXPIRED",
            underlying,
            requested_expiry,
            expiry=expiry_day.isoformat(),
            expiry_dte=dte,
        )

    requested_day = None if _is_normal_request(requested_expiry) else _parse_expiry(requested_expiry)
    if requested_day is not None and expiry_day != requested_day:
        return _failure(
            "EXPIRY_MISMATCH",
            underlying,
            requested_expiry,
            expiry=expiry_day.isoformat(),
            expected_expiry=requested_day.isoformat(),
            expiry_dte=dte,
        )

    max_dte = _max_normal_dte(underlying)
    if _is_normal_request(requested_expiry) and dte > max_dte:
        return _failure(
            "EXPIRY_TOO_FAR",
            underlying,
            requested_expiry,
            expiry=expiry_day.isoformat(),
            expiry_dte=dte,
            max_expiry_dte=max_dte,
        )

    output = dict(result)
    output["success"] = True
    output["expiry"] = expiry_day.isoformat()
    output["expiry_dte"] = dte
    output["max_expiry_dte"] = max_dte
    output["resolver_version"] = VERSION
    return output


def _angel_exact_expiry(
    underlying: Any,
    requested_expiry: Any,
    strike: Any,
    option_type: Any,
) -> dict:
    from bot import option_chain

    name = _normal_underlying(underlying)
    side = str(option_type or "").upper().strip()
    today = _today_ist()

    if name not in option_chain.STRIKE_STEP or side not in {"CE", "PE"}:
        return _failure("UNSUPPORTED_OPTION_REQUEST", name, requested_expiry)

    if _is_normal_request(requested_expiry):
        resolved = option_chain.resolve_option(
            name,
            float(strike),
            side,
        )
        if not resolved:
            return _failure(
                "ANGEL_NEAREST_EXPIRY_NOT_AVAILABLE",
                name,
                requested_expiry,
            )
        output = dict(resolved)
        output["success"] = True
        output["expiry_source"] = "ANGEL_ACTIVE_MASTER_NEAREST"
        return _validate_result(output, name, requested_expiry, today=today)

    requested_day = _parse_expiry(requested_expiry)
    if requested_day is None:
        return _failure("EXPIRY_REQUEST_INVALID", name, requested_expiry)

    candidates = []
    for raw in option_chain._load_cache() or []:
        row = dict(raw or {})
        if str(row.get("name") or "").upper() != name:
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol.endswith(side):
            continue
        expiry_day = option_chain._parse_expiry(row.get("expiry"))
        if expiry_day != requested_day or expiry_day < today:
            continue
        row_strike = option_chain._strike_of(row)
        if row_strike is None:
            continue
        candidates.append((abs(float(row_strike) - float(strike)), float(row_strike), row))

    if not candidates:
        return _failure(
            "ANGEL_EXACT_EXPIRY_NOT_AVAILABLE",
            name,
            requested_expiry,
            expected_expiry=requested_day.isoformat(),
        )

    _, row_strike, best = min(candidates, key=lambda item: (item[0], item[1]))
    result = {
        "success": True,
        "symbol": str(best.get("symbol") or ""),
        "token": str(best.get("token") or ""),
        "exchange": str(best.get("exch_seg") or option_chain.EXCHANGE_FOR.get(name, "NFO")),
        "exch_seg": str(best.get("exch_seg") or option_chain.EXCHANGE_FOR.get(name, "NFO")),
        "expiry": requested_day.isoformat(),
        "strike": row_strike,
        "lot_size": option_chain._lot_size_of(best, name),
        "underlying": name,
        "option_type": side,
        "expiry_source": "ANGEL_ACTIVE_MASTER_EXACT",
    }
    return _validate_result(result, name, requested_expiry, today=today)


def _zerodha_nearest_expiry(
    obj: Any,
    underlying: Any,
    requested_expiry: Any,
    strike: Any,
    option_type: Any,
) -> dict:
    name = _normal_underlying(underlying)
    side = str(option_type or "").upper().strip()
    today = _today_ist()
    exchange = "BFO" if name == "SENSEX" else "NFO"
    requested_day = None if _is_normal_request(requested_expiry) else _parse_expiry(requested_expiry)

    if side not in {"CE", "PE"}:
        return _failure("UNSUPPORTED_OPTION_TYPE", name, requested_expiry)
    if not _is_normal_request(requested_expiry) and requested_day is None:
        return _failure("EXPIRY_REQUEST_INVALID", name, requested_expiry)

    eligible = []
    try:
        instruments = list(obj.kite.instruments(exchange) or [])
    except Exception as exc:
        return _failure(
            "ZERODHA_INSTRUMENTS_UNAVAILABLE",
            name,
            requested_expiry,
            error=str(exc)[:180],
        )

    for raw in instruments:
        row = dict(raw or {})
        if _normal_underlying(row.get("name")) != name:
            continue
        if str(row.get("instrument_type") or "").upper() != side:
            continue
        expiry_day = _parse_expiry(row.get("expiry"))
        if expiry_day is None or expiry_day < today:
            continue
        if requested_day is not None and expiry_day != requested_day:
            continue
        try:
            row_strike = float(row.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if row_strike <= 0:
            continue
        eligible.append((expiry_day, abs(row_strike - float(strike)), row_strike, row))

    if not eligible:
        return _failure(
            "ZERODHA_OPTION_NOT_FOUND",
            name,
            requested_expiry,
            expected_expiry=requested_day.isoformat() if requested_day else None,
        )

    if requested_day is None:
        nearest_expiry = min(item[0] for item in eligible)
        eligible = [item for item in eligible if item[0] == nearest_expiry]

    expiry_day, _, row_strike, best = min(
        eligible,
        key=lambda item: (item[0], item[1], item[2]),
    )
    result = {
        "success": True,
        "symbol": str(best.get("tradingsymbol") or ""),
        "token": str(best.get("instrument_token") or ""),
        "exchange": exchange,
        "expiry": expiry_day.isoformat(),
        "strike": row_strike,
        "lot_size": int(best.get("lot_size") or 0),
        "underlying": name,
        "option_type": side,
        "expiry_source": (
            "ZERODHA_EXACT_EXPIRY"
            if requested_day is not None
            else "ZERODHA_NEAREST_LIVE_EXPIRY"
        ),
    }
    return _validate_result(result, name, requested_expiry, today=today)


def install(angel_class, zerodha_class, upstox_class) -> bool:
    """Install one validated expiry policy across all connected brokers."""
    if not getattr(angel_class, "_okai_expiry_guard_v1", False):
        def angel_search(self, underlying, expiry, strike, option_type):
            return _angel_exact_expiry(
                underlying,
                expiry,
                strike,
                option_type,
            )

        angel_class.search_option = angel_search
        angel_class._okai_expiry_guard_v1 = True
        angel_class._okai_expiry_guard_version = VERSION

    if not getattr(zerodha_class, "_okai_expiry_guard_v1", False):
        def zerodha_search(self, underlying, expiry, strike, option_type):
            return _zerodha_nearest_expiry(
                self,
                underlying,
                expiry,
                strike,
                option_type,
            )

        zerodha_class.search_option = zerodha_search
        zerodha_class._okai_expiry_guard_v1 = True
        zerodha_class._okai_expiry_guard_version = VERSION

    if not getattr(upstox_class, "_okai_expiry_guard_v1", False):
        original_upstox_search = upstox_class.search_option

        def upstox_search(self, underlying, expiry, strike, option_type):
            result = original_upstox_search(
                self,
                underlying,
                expiry,
                strike,
                option_type,
            )
            return _validate_result(
                result,
                underlying,
                expiry,
            )

        upstox_class.search_option = upstox_search
        upstox_class._okai_expiry_guard_v1 = True
        upstox_class._okai_expiry_guard_version = VERSION

    return True
