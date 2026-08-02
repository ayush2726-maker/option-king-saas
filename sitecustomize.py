"""Runtime compatibility for Angel One SmartAPI symbol search.

Option King AI pins smartapi-python 1.3.4. Some SmartConnect builds do not
expose ``searchScrip`` even though the sector-rotation resolver needs the same
symbol-to-token lookup. Python imports ``sitecustomize`` automatically during
normal interpreter startup, so this module adds the missing method without
changing any order, risk, strategy, or broker-session logic.
"""

from __future__ import annotations

import threading
import time
from typing import Any

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)
CACHE_TTL_SECONDS = 12 * 60 * 60

_master_lock = threading.Lock()
_master_rows: list[dict[str, str]] = []
_master_loaded_at = 0.0


def _normalize(value: Any) -> str:
    return str(value or "").strip().upper()


def _download_equity_master() -> list[dict[str, str]]:
    import requests

    response = requests.get(
        SCRIP_MASTER_URL,
        headers={"User-Agent": "OptionKingAI/1.0"},
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()

    rows: list[dict[str, str]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue

        exchange = _normalize(item.get("exch_seg"))
        symbol = _normalize(item.get("symbol"))
        token = str(item.get("token") or "").strip()
        name = _normalize(item.get("name"))

        if exchange not in {"NSE", "BSE"} or not symbol or not token:
            continue

        # Keep cash-market equities and indices only. Derivatives are resolved
        # by the existing option-chain module and must not be mixed in here.
        instrument_type = _normalize(item.get("instrumenttype"))
        is_cash_equity = symbol.endswith("-EQ") or instrument_type in {
            "",
            "EQ",
            "AMXIDX",
            "INDEX",
        }
        if not is_cash_equity:
            continue

        rows.append(
            {
                "exchange": exchange,
                "tradingsymbol": symbol,
                "symboltoken": token,
                "name": name,
            }
        )

    if not rows:
        raise RuntimeError("Angel instrument master returned no cash-market rows")
    return rows


def _load_master() -> list[dict[str, str]]:
    global _master_rows, _master_loaded_at

    now = time.monotonic()
    with _master_lock:
        if _master_rows and now - _master_loaded_at < CACHE_TTL_SECONDS:
            return _master_rows

        stale = _master_rows
        try:
            fresh = _download_equity_master()
        except Exception:
            if stale:
                return stale
            raise

        _master_rows = fresh
        _master_loaded_at = now
        return _master_rows


def _search_master(exchange: Any, query: Any) -> list[dict[str, str]]:
    exchange_key = _normalize(exchange)
    search_key = _normalize(query)
    if not exchange_key or not search_key:
        return []

    candidates: list[tuple[int, str, dict[str, str]]] = []
    for row in _load_master():
        if row["exchange"] != exchange_key:
            continue

        symbol = row["tradingsymbol"]
        name = row.get("name", "")
        base_symbol = symbol[:-3] if symbol.endswith("-EQ") else symbol

        if base_symbol == search_key or symbol == search_key:
            rank = 0
        elif name == search_key:
            rank = 1
        elif base_symbol.startswith(search_key) or name.startswith(search_key):
            rank = 2
        elif search_key in base_symbol or search_key in name:
            rank = 3
        else:
            continue

        candidates.append((rank, symbol, row))

    candidates.sort(key=lambda item: (item[0], item[1]))
    return [
        {
            "exchange": row["exchange"],
            "tradingsymbol": row["tradingsymbol"],
            "symboltoken": row["symboltoken"],
        }
        for _, _, row in candidates[:50]
    ]


def _compat_search_scrip(self: Any, exchange: Any, searchscrip: Any) -> dict[str, Any]:
    try:
        data = _search_master(exchange, searchscrip)
        return {
            "status": bool(data),
            "message": "SUCCESS" if data else "No matching Angel instrument",
            "errorcode": "" if data else "NO_MATCH",
            "data": data,
        }
    except Exception as exc:
        return {
            "status": False,
            "message": f"Angel instrument lookup failed: {str(exc)[:180]}",
            "errorcode": "INSTRUMENT_MASTER_UNAVAILABLE",
            "data": [],
        }


def _install() -> None:
    try:
        from SmartApi import SmartConnect
    except Exception:
        return

    if not hasattr(SmartConnect, "searchScrip"):
        SmartConnect.searchScrip = _compat_search_scrip
        SmartConnect.__okai_search_scrip_compat__ = True


_install()
