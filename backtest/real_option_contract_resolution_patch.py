"""Prefer currently active contracts before calling Plus-only expired APIs.

A completed recent trading day can refer to an option whose expiry is still
active today. Such a backtest should work with the normal Option Contracts and
Historical Candle APIs. Older dates then use the Upstox Plus expired APIs.
"""

from datetime import datetime

from bot.brokers.upstox import UpstoxBroker
from backtest.real_option_premium_patch import (
    MAX_EXPIRY_GAP_DAYS,
    UNDERLYING_KEYS,
    _f,
)


def _date(value):
    return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()


def _select_atm(contracts, underlying, underlying_key, trade_day, spot, side, expired):
    matching = [
        row
        for row in contracts or []
        if str(row.get("option_type") or "").upper() == side
    ]
    if not matching:
        return {
            "success": False,
            "message": "REAL_PREMIUM_OPTION_CONTRACT_NOT_FOUND",
            "side": side,
        }

    selected = min(
        matching,
        key=lambda row: (
            abs(_f(row.get("strike"), 0) - spot),
            _f(row.get("strike"), 0),
        ),
    )
    expiry_day = _date(selected.get("expiry"))
    return {
        "success": True,
        **selected,
        "underlying": underlying,
        "underlying_key": underlying_key,
        "expired": bool(expired),
        "selection": "NEAREST_ATM_STRIKE_NEAREST_VALID_EXPIRY",
        "expiry_gap_days": (expiry_day - trade_day).days,
    }


def _resolve_historical_option_active_first(
    self,
    underlying,
    trade_date,
    spot_price,
    option_type,
):
    name = str(underlying or "").upper()
    side = str(option_type or "").upper()
    underlying_key = UNDERLYING_KEYS.get(name)
    if not underlying_key or side not in ("CE", "PE"):
        return {"success": False, "message": "REAL_PREMIUM_UNSUPPORTED_INSTRUMENT"}

    try:
        trade_day = _date(trade_date)
    except Exception:
        return {"success": False, "message": "REAL_PREMIUM_INVALID_DATE"}

    spot = _f(spot_price, 0)
    max_gap = MAX_EXPIRY_GAP_DAYS.get(name, 10)

    active_result = self.get_active_option_contracts(underlying_key)
    active_contracts = (
        active_result.get("contracts") or []
        if active_result.get("success")
        else []
    )
    active_expiries = []
    for row in active_contracts:
        try:
            expiry_day = _date(row.get("expiry"))
        except Exception:
            continue
        if expiry_day >= trade_day and (expiry_day - trade_day).days <= max_gap:
            active_expiries.append((expiry_day, str(row.get("expiry"))))

    if active_expiries:
        _, expiry_text = min(active_expiries, key=lambda row: row[0])
        return _select_atm(
            [row for row in active_contracts if row.get("expiry") == expiry_text],
            name,
            underlying_key,
            trade_day,
            spot,
            side,
            False,
        )

    # The contract is no longer in the active master; use exact expired data.
    expired_result = self.get_expired_expiries(underlying_key)
    if not expired_result.get("success"):
        return expired_result

    candidates = []
    for expiry_text in expired_result.get("expiries") or []:
        try:
            expiry_day = _date(expiry_text)
        except Exception:
            continue
        gap = (expiry_day - trade_day).days
        if 0 <= gap <= max_gap:
            candidates.append((expiry_day, expiry_text))

    if not candidates:
        return {
            "success": False,
            "message": "REAL_PREMIUM_EXPIRY_NOT_AVAILABLE_FOR_DATE",
            "trade_date": str(trade_date)[:10],
        }

    expiry_day, expiry_text = min(candidates, key=lambda row: row[0])
    contract_result = self.get_option_contracts_for_expiry(
        underlying_key,
        expiry_text,
        True,
    )
    if not contract_result.get("success"):
        return contract_result

    selected = _select_atm(
        [
            row
            for row in contract_result.get("contracts") or []
            if row.get("expiry") == expiry_text
        ],
        name,
        underlying_key,
        trade_day,
        spot,
        side,
        True,
    )
    if selected.get("success"):
        selected["expiry_gap_days"] = (expiry_day - trade_day).days
    return selected


def apply_real_option_contract_resolution_patch():
    UpstoxBroker.resolve_historical_option = (
        _resolve_historical_option_active_first
    )
