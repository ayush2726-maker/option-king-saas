"""Correct Upstox previous-close mapping for Sector Rotation.

Upstox Full Market Quotes documents ``net_change`` as the absolute difference
between yesterday's close and the current last traded price. The legacy Sector
Rotation fetcher preferred ``ohlc.close`` first. During a live session that field
can represent the most recent live candle close and equal LTP, producing 0.00%
for every stock even while prices are moving.

This display-data patch makes ``previous_close = last_price - net_change`` the
primary Upstox basis and keeps OHLC/percentage fallbacks for older payload shapes.
It never changes strategy scores, entries, exits, risk, quantities, or orders.
"""
from __future__ import annotations

import math
import sys
import threading
import time
from typing import Any, Mapping, Optional

VERSION = "OKAI-UPSTOX-SECTOR-NET-CHANGE-V1"
_PATCH_LOCK = threading.RLock()
_PATCHED = False
_WATCHER_STARTED = False
_LAST_ERROR: Optional[str] = None


def _f(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _pick(mapping: Mapping[str, Any], *keys: str) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, ""):
            return mapping.get(key)
    return None


def _upstox_change_basis(entry: Mapping[str, Any]) -> dict[str, Any]:
    entry = dict(entry or {})
    ohlc = dict(entry.get("ohlc") or {})
    ltpc = dict(entry.get("ltpc") or {})

    ltp = _f(
        _pick(
            entry,
            "last_price",
            "ltp",
            "lastPrice",
            "last_traded_price",
        )
    )
    if ltp is None:
        ltp = _f(_pick(ltpc, "ltp", "last_price", "lastPrice"))

    net_change = _f(
        _pick(
            entry,
            "net_change",
            "netChange",
            "absolute_change",
            "day_change",
        )
    )
    percent = _f(
        _pick(
            entry,
            "percent_change",
            "change_percent",
            "percentage_change",
            "pChange",
        )
    )

    close_candidate = _f(
        _pick(
            ohlc,
            "previous_close",
            "prev_close",
            "close_price",
            "close",
            "cp",
        )
    )
    if close_candidate is None:
        close_candidate = _f(
            _pick(
                entry,
                "previous_close",
                "prev_close",
                "close_price",
                "close",
                "cp",
            )
        )
    if close_candidate is None:
        close_candidate = _f(_pick(ltpc, "cp", "close", "previous_close"))

    previous_close = None
    source = "unavailable"

    # Upstox Full Market Quotes: net_change = LTP - yesterday close.
    # Use this before live OHLC close, which can equal the current LTP.
    if ltp is not None and net_change is not None:
        derived = ltp - net_change
        if derived > 0:
            previous_close = derived
            source = "upstox_net_change"

    if previous_close is None and ltp is not None and percent not in (None, -100.0):
        divisor = 1.0 + percent / 100.0
        if divisor > 0:
            derived = ltp / divisor
            if derived > 0:
                previous_close = derived
                source = "upstox_percent_change"

    if previous_close is None and close_candidate not in (None, 0.0):
        previous_close = close_candidate
        source = "upstox_ohlc_close_fallback"

    calculated_percent = percent
    if ltp is not None and previous_close not in (None, 0.0):
        calculated_percent = ((ltp - previous_close) / previous_close) * 100.0

    return {
        "ltp": ltp,
        "previous_close": previous_close,
        "change_percent": calculated_percent,
        "net_change": net_change,
        "change_source": source,
    }


def _patched_fetcher(routes):
    def fetch_upstox_quotes(user_id, creds, universe):
        obj = routes._get_multi_session(user_id, "upstox", creds)
        key_by_symbol = {}
        for row in universe:
            key = routes._resolve_upstox_key(obj, row["symbol"])
            if key:
                key_by_symbol[row["symbol"]] = key

        if not key_by_symbol:
            raise RuntimeError("Upstox equity instruments could not be resolved")

        response = routes.requests.get(
            f"{obj.BASE_URL}/market-quote/quotes",
            params={"instrument_key": ",".join(key_by_symbol.values())},
            headers=obj._h(),
            timeout=15,
        )
        payload = response.json()
        if response.status_code != 200 or payload.get("status") != "success":
            raise RuntimeError(
                str(payload.get("errors") or payload.get("message") or payload)[:220]
            )

        data = payload.get("data") or {}
        by_symbol = {}
        by_token = {}
        by_key = {}
        for response_key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            response_key = str(response_key or "").strip()
            entry_symbol = str(
                entry.get("symbol")
                or entry.get("trading_symbol")
                or entry.get("tradingSymbol")
                or ""
            ).strip().upper()
            token = str(entry.get("instrument_token") or "").strip()
            if response_key:
                by_key[response_key] = entry
                by_key[response_key.replace(":", "|", 1)] = entry
            if entry_symbol:
                by_symbol[entry_symbol] = entry
            if token:
                by_token[token] = entry

        output = {}
        for row in universe:
            symbol = row["symbol"]
            key = key_by_symbol.get(symbol)
            entry = (
                by_symbol.get(symbol)
                or by_key.get(str(key or ""))
                or by_key.get(str(key or "").replace("|", ":", 1))
                or by_token.get(str(key or ""))
                or {}
            )
            basis = _upstox_change_basis(entry)
            quote = routes._quote_row(
                symbol,
                row["name"],
                row["sector"],
                basis["ltp"],
                basis["previous_close"],
                basis["change_percent"],
            )
            quote["change_source"] = basis["change_source"]
            quote["raw_net_change"] = (
                round(basis["net_change"], 4)
                if basis["net_change"] is not None
                else None
            )
            output[symbol] = quote
        return output

    fetch_upstox_quotes._okai_upstox_sector_net_change_v1 = True
    return fetch_upstox_quotes


def apply_upstox_sector_change_fix() -> bool:
    global _PATCHED, _LAST_ERROR
    with _PATCH_LOCK:
        if _PATCHED:
            return True
        routes = sys.modules.get("bot.sector_rotation_routes")
        if routes is None or not all(
            hasattr(routes, name)
            for name in (
                "_get_multi_session",
                "_resolve_upstox_key",
                "_quote_row",
                "requests",
            )
        ):
            return False
        try:
            current = routes._fetch_upstox_quotes
            if getattr(current, "_okai_upstox_sector_net_change_v1", False):
                _PATCHED = True
                return True
            routes._fetch_upstox_quotes = _patched_fetcher(routes)
            routes._rotation_cache.clear()
            routes.UPSTOX_SECTOR_CHANGE_VERSION = VERSION
            _PATCHED = True
            _LAST_ERROR = None
            print(
                f"UPSTOX SECTOR CHANGE {VERSION} active | previous close from net_change | orders OFF"
            )
            return True
        except Exception as exc:
            _LAST_ERROR = f"{type(exc).__name__}:{str(exc)[:220]}"
            return False


def _watch() -> None:
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if apply_upstox_sector_change_fix():
            return
        time.sleep(0.05)


def schedule_upstox_sector_change_fix() -> None:
    global _WATCHER_STARTED
    with _PATCH_LOCK:
        if _WATCHER_STARTED:
            return
        _WATCHER_STARTED = True
        threading.Thread(
            target=_watch,
            name="okai-upstox-sector-change-v1-loader",
            daemon=True,
        ).start()


__all__ = [
    "VERSION",
    "_upstox_change_basis",
    "apply_upstox_sector_change_fix",
    "schedule_upstox_sector_change_fix",
]
