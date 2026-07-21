"""Upstox live candle compatibility patch for AUTO portfolio scans.

The Upstox broker adapter already returns candles sorted oldest-to-newest, but
the legacy multi-broker helper reversed them again. AUTO scans also request
three indices back-to-back. This wrapper keeps chronological order, adds light
request pacing/retry, and reuses a recent successful snapshot during transient
API failures.
"""

import threading
import time

from bot import angel_fetcher


_lock = threading.Lock()
_last_request_at = 0.0
_cache = {}
CACHE_SECONDS = 75
MIN_REQUEST_GAP_SECONDS = 0.55


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


def _get_upstox_candles(broker_obj, underlying):
    from datetime import datetime, timezone, timedelta

    key = angel_fetcher.UPSTOX_INDEX_KEYS[underlying]
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    today = now_ist.strftime("%Y-%m-%d")
    last_error = "Upstox candles unavailable"

    for attempt in range(2):
        _pace_request()
        result = broker_obj.get_candles(
            symbol=key,
            interval="1m",
            from_date=today,
            to_date=today,
        )
        if isinstance(result, dict) and result.get("success"):
            frame = _normalise_upstox_rows(result.get("candles", []))
            if frame is not None and len(frame) > 0:
                _cache[underlying] = (time.monotonic(), frame.copy())
                return frame
            last_error = "Upstox returned empty intraday candles"
        else:
            last_error = str(
                (result or {}).get("message", "Upstox candle fetch failed")
            )[:240]

        if attempt == 0:
            time.sleep(0.8)

    cached = _cached(underlying)
    if cached is not None:
        return cached

    raise RuntimeError(last_error)


def apply_upstox_live_candle_patch():
    if getattr(angel_fetcher, "_okai_upstox_live_patch_v1", False):
        return

    original = angel_fetcher.get_candles_multi

    def patched_get_candles_multi(broker_name, broker_obj, underlying):
        if str(broker_name).lower() != "upstox":
            return original(broker_name, broker_obj, underlying)
        return _get_upstox_candles(broker_obj, str(underlying).upper())

    angel_fetcher.get_candles_multi = patched_get_candles_multi
    angel_fetcher._okai_upstox_live_patch_v1 = True
