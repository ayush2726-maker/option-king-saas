"""Recover AUTO live scans from the one-day broker history cache.

Upstox can intermittently return an empty intraday response for one index while
another index succeeds. The graph endpoint may already have a valid cached
one-day candle set. This patch lets the AUTO scanner reuse that same cache when
the direct live request is missing, short, or temporarily rate-limited.
"""

import math

from bot import auto_portfolio_runtime as runtime
from bot.history_provider import get_historical_rows


def _frame_from_rows(rows):
    import pandas as pd

    if not rows:
        return None

    normalised = []
    for row in rows:
        if isinstance(row, dict):
            values = [
                row.get("time") or row.get("date") or row.get("timestamp"),
                row.get("open"),
                row.get("high"),
                row.get("low"),
                row.get("close"),
                row.get("volume", 0),
            ]
        elif isinstance(row, (list, tuple)) and len(row) >= 6:
            values = list(row[:6])
        else:
            continue
        normalised.append(values)

    if not normalised:
        return None

    frame = pd.DataFrame(
        normalised,
        columns=["time", "open", "high", "low", "close", "volume"],
    )
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce", utc=True)
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = (
        frame.dropna(subset=["time", "open", "high", "low", "close"])
        .drop_duplicates(subset=["time"], keep="last")
        .sort_values("time")
        .reset_index(drop=True)
    )
    frame = frame[
        frame[["open", "high", "low", "close"]]
        .applymap(lambda value: math.isfinite(float(value)))
        .all(axis=1)
    ]
    return frame if not frame.empty else None


def apply_live_scan_history_fallback_patch():
    if getattr(runtime, "_okai_live_history_fallback_v1", False):
        return

    def patched_scan_multi(user_id, broker_name, obj, settings, profile, streak):
        scans = []

        for underlying in runtime._enabled(settings):
            frame = None
            direct_error = None
            fallback_reason = None
            used_fallback = False

            try:
                frame = runtime._legacy().get_candles_multi(
                    broker_name,
                    obj,
                    underlying,
                )
            except Exception as exc:
                direct_error = str(exc)[:200]

            if frame is None or len(frame) < 28:
                try:
                    rows, fallback_reason, _, _ = get_historical_rows(
                        user_id,
                        underlying,
                        1,
                    )
                    cached_frame = _frame_from_rows(rows)
                    if cached_frame is not None and len(cached_frame) >= 28:
                        frame = cached_frame
                        used_fallback = True
                except Exception as exc:
                    fallback_reason = str(exc)[:200]

            if frame is None or len(frame) < 28:
                count = len(frame) if frame is not None else 0
                scans.append({
                    "underlying": underlying,
                    "status": "WAITING_CANDLES",
                    "error": (
                        direct_error
                        or fallback_reason
                        or f"Only {count} usable candles"
                    ),
                    "candle_count": count,
                })
                continue

            try:
                scan = runtime._build_scan(
                    user_id,
                    underlying,
                    frame,
                    profile,
                    streak,
                )
                if used_fallback:
                    scan["data_source"] = "HISTORY_CACHE_FALLBACK"
                scans.append(scan)
            except Exception as exc:
                scans.append({
                    "underlying": underlying,
                    "status": "ERROR",
                    "error": str(exc)[:200],
                })

        return scans

    runtime._scan_multi = patched_scan_multi
    runtime._okai_live_history_fallback_v1 = True
