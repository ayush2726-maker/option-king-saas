"""Upstox live candle compatibility for AUTO portfolio scans.

Keeps candles chronological, paces requests, retries transient failures and
resolves current index instrument keys dynamically when a static key returns an
empty response.
"""

import threading
import time

from bot import angel_fetcher


_lock = threading.Lock()
_last_request_at = 0.0
_cache = {}
_key_cache = {}
CACHE_SECONDS = 90
MIN_REQUEST_GAP_SECONDS = 0.65


_INDEX_SEARCH = {
    "NIFTY": {
        "query": "Nifty 50",
        "exchange": "NSE",
        "names": {"NIFTY", "NIFTY 50"},
    },
    "BANKNIFTY": {
        "query": "Nifty Bank",
        "exchange": "NSE",
        "names": {"BANKNIFTY", "NIFTY BANK"},
    },
    "SENSEX": {
        "query": "SENSEX",
        "exchange": "BSE",
        "names": {"SENSEX"},
    },
}


def _pace_request():
    global _last_request_at
    with _lock:
        now = time.monotonic()
        wait_for = MIN_REQUEST_GAP_SECONDS - (now - _last_request_at)
        if wait_for > 0:
            time.sleep(wait_for)
        _last_request_at = time.monotonic()


def _cached(underlying):
    item = _cache.get(str(underlying).upper())
    if not item:
        return None
    timestamp, frame = item
    if time.monotonic() - timestamp > CACHE_SECONDS:
        return None
    try:
        return frame.copy()
    except Exception:
        return frame


def _normalise_upstox_rows(rows):
    import pandas as pd

    if not rows:
        return None

    normalised = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        normalised.append(list(row[:6]))

    if not normalised:
        return None

    df = pd.DataFrame(
        normalised,
        columns=["time", "open", "high", "low", "close", "volume"],
    )
    df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = (
        df.dropna(subset=["time", "open", "high", "low", "close"])
        .drop_duplicates(subset=["time"], keep="last")
        .sort_values("time")
        .reset_index(drop=True)
    )
    return df if not df.empty else None


def _resolve_index_key(broker_obj, underlying):
    underlying = str(underlying).upper()
    if underlying in _key_cache:
        return _key_cache[underlying]

    static_key = angel_fetcher.UPSTOX_INDEX_KEYS.get(underlying)
    config = _INDEX_SEARCH.get(underlying)
    if not config:
        return static_key

    try:
        import requests

        _pace_request()
        response = requests.get(
            f"{broker_obj.BASE_URL}/instruments/search",
            params={
                "query": config["query"],
                "exchanges": config["exchange"],
                "segments": "INDEX",
                "instrument_types": "INDEX",
                "page_number": 1,
                "records": 50,
            },
            headers=broker_obj._h(),
            timeout=15,
        )
        payload = response.json()
        rows = payload.get("data") or []

        best_key = None
        for row in rows:
            if str(row.get("instrument_type") or "").upper() != "INDEX":
                continue
            name = str(row.get("name") or "").upper().strip()
            symbol = str(row.get("trading_symbol") or "").upper().strip()
            key = str(row.get("instrument_key") or "").strip()
            if not key:
                continue
            if name in config["names"] or symbol in config["names"]:
                best_key = key
                break

        if best_key:
            _key_cache[underlying] = best_key
            return best_key
    except Exception:
        pass

    return static_key


def _fetch_frame(broker_obj, key, today):
    _pace_request()
    result = broker_obj.get_candles(
        symbol=key,
        interval="1m",
        from_date=today,
        to_date=today,
    )
    if isinstance(result, dict) and result.get("success"):
        return _normalise_upstox_rows(result.get("candles", [])), None
    return None, str(
        (result or {}).get("message", "Upstox candle fetch failed")
    )[:240]


def _get_upstox_candles(broker_obj, underlying):
    from datetime import datetime, timezone, timedelta

    underlying = str(underlying).upper()
    static_key = angel_fetcher.UPSTOX_INDEX_KEYS[underlying]
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    today = now_ist.strftime("%Y-%m-%d")
    last_error = "Upstox candles unavailable"

    keys = [static_key]
    dynamic_key = _resolve_index_key(broker_obj, underlying)
    if dynamic_key and dynamic_key not in keys:
        keys.append(dynamic_key)

    for key in keys:
        for attempt in range(2):
            frame, error = _fetch_frame(broker_obj, key, today)
            if frame is not None and len(frame) > 0:
                _key_cache[underlying] = key
                _cache[underlying] = (time.monotonic(), frame.copy())
                return frame

            last_error = error or f"Upstox returned empty intraday candles for {key}"
            if attempt == 0:
                time.sleep(0.8)

    # A static key may return a successful-but-empty response. Force a fresh
    # instrument search once before giving up.
    _key_cache.pop(underlying, None)
    refreshed_key = _resolve_index_key(broker_obj, underlying)
    if refreshed_key and refreshed_key not in keys:
        frame, error = _fetch_frame(broker_obj, refreshed_key, today)
        if frame is not None and len(frame) > 0:
            _key_cache[underlying] = refreshed_key
            _cache[underlying] = (time.monotonic(), frame.copy())
            return frame
        last_error = error or last_error

    cached = _cached(underlying)
    if cached is not None:
        return cached

    raise RuntimeError(last_error)


def apply_upstox_live_candle_patch():
    if getattr(angel_fetcher, "_okai_upstox_live_patch_v2", False):
        return

    original = angel_fetcher.get_candles_multi

    def patched_get_candles_multi(broker_name, broker_obj, underlying):
        if str(broker_name).lower() != "upstox":
            return original(broker_name, broker_obj, underlying)
        return _get_upstox_candles(broker_obj, str(underlying).upper())

    angel_fetcher.get_candles_multi = patched_get_candles_multi
    angel_fetcher._okai_upstox_live_patch_v2 = True
