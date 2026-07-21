"""Reliable candle recovery for AUTO NIFTY/BANKNIFTY/SENSEX scans.

The AUTO engine must not show a false 0 score merely because one Upstox
intraday request is temporarily empty.  This patch tries, in order:

1. the normal live broker request,
2. the shared one-day historical provider/cache used by the chart endpoint,
3. Upstox's date-range historical endpoint,
4. the last recent successful frame for that user/instrument.

Every result also carries candle_count/data_source/error diagnostics so the app
can report the real reason instead of an unexplained WAITING_CANDLES status.
"""

from datetime import datetime, timedelta, timezone
import math
import time

from bot import auto_portfolio_runtime as runtime
from bot import history_provider


_LAST_GOOD_FRAMES = {}
LAST_GOOD_SECONDS = 180
MIN_CANDLES = 28


def _copy_frame(frame):
    if frame is None:
        return None
    try:
        return frame.copy()
    except Exception:
        return frame


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

    # Do not use DataFrame.applymap here.  Some Railway pandas builds remove or
    # deprecate it.  A simple row loop is compatible with all supported builds.
    valid_indexes = []
    for index, row in frame.iterrows():
        try:
            values = [float(row[name]) for name in ("open", "high", "low", "close")]
            if all(math.isfinite(value) and value > 0 for value in values):
                valid_indexes.append(index)
        except Exception:
            continue

    if not valid_indexes:
        return None

    frame = frame.loc[valid_indexes].reset_index(drop=True)
    return frame if not frame.empty else None


def _provider_frame(user_id, underlying):
    notes = []

    try:
        rows, reason, broker_name, cached = history_provider.get_historical_rows(
            user_id,
            underlying,
            1,
        )
        frame = _frame_from_rows(rows)
        notes.append(
            f"provider={broker_name or 'none'} cached={bool(cached)} rows={len(rows or [])}"
        )
        if reason:
            notes.append(str(reason)[:120])
        if frame is not None and len(frame) >= MIN_CANDLES:
            return frame, "HISTORY_PROVIDER", " | ".join(notes)
    except Exception as exc:
        notes.append("provider_error=" + str(exc)[:140])

    # Read the already-populated chart cache directly.  This bypasses a short
    # error-cache window when the chart has valid candles but an earlier live
    # request recorded a transient failure.
    try:
        cache_key = (int(user_id), str(underlying).upper(), 1)
        cached_item = getattr(history_provider, "_CACHE", {}).get(cache_key) or {}
        cached_rows = cached_item.get("rows") or []
        frame = _frame_from_rows(cached_rows)
        notes.append(f"direct_cache_rows={len(cached_rows)}")
        if frame is not None and len(frame) >= MIN_CANDLES:
            return frame, "CHART_CACHE_DIRECT", " | ".join(notes)
    except Exception as exc:
        notes.append("cache_error=" + str(exc)[:120])

    return None, None, " | ".join(notes)


def _upstox_range_frame(broker_obj, underlying):
    """Force the date-range endpoint instead of same-day intraday endpoint."""
    try:
        from bot.angel_fetcher import UPSTOX_INDEX_KEYS

        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        today = now_ist.strftime("%Y-%m-%d")
        yesterday = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")
        result = broker_obj.get_candles(
            symbol=UPSTOX_INDEX_KEYS[str(underlying).upper()],
            interval="1m",
            from_date=yesterday,
            to_date=today,
        )
        if not isinstance(result, dict) or not result.get("success"):
            return None, str((result or {}).get("message") or "range request failed")[:160]

        rows = result.get("candles") or []
        frame = _frame_from_rows(rows)
        if frame is None:
            return None, f"range rows unusable={len(rows)}"

        # Keep the current IST trading day when possible.  If timestamp
        # conversion differs by broker, retaining the latest rows is safer than
        # returning no score at all.
        today_rows = frame[
            frame["time"].dt.tz_convert("Asia/Kolkata").dt.strftime("%Y-%m-%d") == today
        ].reset_index(drop=True)
        if len(today_rows) >= MIN_CANDLES:
            frame = today_rows

        return frame, f"range_rows={len(frame)}"
    except Exception as exc:
        return None, str(exc)[:160]


def _recent_frame(user_id, underlying):
    key = (int(user_id), str(underlying).upper())
    item = _LAST_GOOD_FRAMES.get(key)
    if not item:
        return None
    saved_at, frame = item
    if time.monotonic() - saved_at > LAST_GOOD_SECONDS:
        return None
    return _copy_frame(frame)


def _remember_frame(user_id, underlying, frame):
    if frame is None or len(frame) < MIN_CANDLES:
        return
    key = (int(user_id), str(underlying).upper())
    _LAST_GOOD_FRAMES[key] = (time.monotonic(), _copy_frame(frame))


def apply_live_scan_history_fallback_patch():
    if getattr(runtime, "_okai_live_history_fallback_v3", False):
        return

    def patched_scan_multi(user_id, broker_name, obj, settings, profile, streak):
        scans = []

        for underlying in runtime._enabled(settings):
            frame = None
            source = None
            notes = []

            try:
                frame = runtime._legacy().get_candles_multi(
                    broker_name,
                    obj,
                    underlying,
                )
                notes.append(f"direct_rows={len(frame) if frame is not None else 0}")
                if frame is not None and len(frame) >= MIN_CANDLES:
                    source = "LIVE_BROKER"
            except Exception as exc:
                notes.append("direct_error=" + str(exc)[:160])

            if frame is None or len(frame) < MIN_CANDLES:
                provider_frame, provider_source, provider_note = _provider_frame(
                    user_id,
                    underlying,
                )
                if provider_note:
                    notes.append(provider_note)
                if provider_frame is not None and len(provider_frame) >= MIN_CANDLES:
                    frame = provider_frame
                    source = provider_source

            if (
                (frame is None or len(frame) < MIN_CANDLES)
                and str(broker_name).lower() == "upstox"
            ):
                range_frame, range_note = _upstox_range_frame(obj, underlying)
                notes.append("upstox_range=" + str(range_note or "")[:180])
                if range_frame is not None and len(range_frame) >= MIN_CANDLES:
                    frame = range_frame
                    source = "UPSTOX_RANGE_HISTORY"

            if frame is None or len(frame) < MIN_CANDLES:
                recent = _recent_frame(user_id, underlying)
                if recent is not None and len(recent) >= MIN_CANDLES:
                    frame = recent
                    source = "LAST_GOOD_FRAME"
                    notes.append(f"last_good_rows={len(recent)}")

            candle_count = len(frame) if frame is not None else 0

            if frame is None or candle_count < MIN_CANDLES:
                scans.append({
                    "underlying": underlying,
                    "status": "WAITING_CANDLES",
                    "signal": "WAIT",
                    "candidate_signal": "WAIT",
                    "score": 0,
                    "min_score": int(profile.get("entry_threshold", 82) or 82),
                    "trade_allowed": False,
                    "candle_count": candle_count,
                    "data_source": source or "NONE",
                    "error": " | ".join(note for note in notes if note)[:500]
                    or f"Only {candle_count} usable candles",
                })
                continue

            try:
                _remember_frame(user_id, underlying, frame)
                scan = runtime._build_scan(
                    user_id,
                    underlying,
                    frame,
                    profile,
                    streak,
                )
                scan["candle_count"] = candle_count
                scan["data_source"] = source or "LIVE_BROKER"
                if notes:
                    scan["data_note"] = " | ".join(notes)[:500]
                scans.append(scan)
            except Exception as exc:
                scans.append({
                    "underlying": underlying,
                    "status": "ERROR",
                    "signal": "WAIT",
                    "candidate_signal": "WAIT",
                    "score": 0,
                    "min_score": int(profile.get("entry_threshold", 82) or 82),
                    "trade_allowed": False,
                    "candle_count": candle_count,
                    "data_source": source or "UNKNOWN",
                    "error": str(exc)[:500],
                })

        return scans

    runtime._scan_multi = patched_scan_multi
    runtime._okai_live_history_fallback_v3 = True
