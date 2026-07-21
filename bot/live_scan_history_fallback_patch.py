"""Reliable candle and scoring recovery for AUTO portfolio scans.

The chart replay path is already able to calculate stable scores from broker
candles. AUTO live scans should use the same sanitised calculation whenever the
legacy live builder raises, returns incomplete indicators, or receives a
transiently short broker response.
"""

from datetime import datetime, timedelta, timezone
import math
import time

from bot import auto_portfolio_runtime as runtime
from bot import history_provider


_LAST_GOOD_FRAMES = {}
LAST_GOOD_SECONDS = 240
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


def _rows_from_frame(frame):
    if frame is None:
        return []
    rows = []
    for _, row in frame.iterrows():
        rows.append([
            row.get("time"),
            row.get("open"),
            row.get("high"),
            row.get("low"),
            row.get("close"),
            row.get("volume", 0),
        ])
    return rows


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
    try:
        from bot.angel_fetcher import UPSTOX_INDEX_KEYS

        key = UPSTOX_INDEX_KEYS[str(underlying).upper()]
        try:
            resolved = broker_obj.resolve_index_key(underlying)
            if isinstance(resolved, dict) and resolved.get("success"):
                key = resolved.get("instrument_key") or key
        except Exception:
            pass

        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        today = now_ist.strftime("%Y-%m-%d")
        yesterday = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")
        result = broker_obj.get_candles(
            symbol=key,
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

        today_rows = frame[
            frame["time"].dt.tz_convert("Asia/Kolkata").dt.strftime("%Y-%m-%d") == today
        ].reset_index(drop=True)
        if len(today_rows) >= MIN_CANDLES:
            frame = today_rows

        return frame, f"range_rows={len(frame)} key={key}"
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


def _safe_number(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except Exception:
        return float(default)


def _replay_aligned_scan(user_id, underlying, frame, profile, source, notes):
    """Build a live scan from the exact sanitised replay calculation."""
    from bot.routes import _historical_indicator_candles

    chart_candles = _historical_indicator_candles(
        _rows_from_frame(frame),
        profile=profile,
    )
    scored = [
        candle
        for candle in chart_candles
        if candle.get("score") is not None
    ]
    if not scored:
        raise RuntimeError("Replay scoring produced no completed score candle")

    # Prefer the last completed candle.  The newest row can still be forming.
    latest = scored[-2] if len(scored) >= 2 else scored[-1]
    latest_index = chart_candles.index(latest)
    previous = chart_candles[max(0, latest_index - 1)]

    candidate = str(latest.get("signal") or "WAIT").upper()
    allowed = bool(latest.get("trade_allowed", False))
    final_signal = candidate if allowed and candidate in ("CE", "PE") else "WAIT"
    ema9 = _safe_number(latest.get("ema9"), latest.get("close"))
    ema21 = _safe_number(latest.get("ema21"), latest.get("close"))
    trend = "UPTREND" if ema9 > ema21 else "DOWNTREND" if ema9 < ema21 else "SIDEWAYS"

    signal_data = {
        "signal": final_signal,
        "candidate_signal": candidate,
        "score": int(_safe_number(latest.get("score"), 0)),
        "min_score": int(_safe_number(latest.get("min_score"), profile.get("entry_threshold", 82))),
        "trade_allowed": allowed,
        "adx": _safe_number(latest.get("adx"), 0),
        "volume_ratio": _safe_number(latest.get("volume_ratio"), 0),
        "mtf_confirmed": trend != "SIDEWAYS",
        "warnings": ["LIVE_REPLAY_ALIGNED_FALLBACK"],
        "strategy": "CUSTOM_PROFILE_V1" if profile.get("profile_key") != "okai_default_82" else "TQU_ENHANCED",
        "strategy_profile_key": profile.get("profile_key", "okai_default_82"),
        "strategy_profile_name": profile.get("profile_name", "OKAI Default 82"),
    }

    market_data = {
        "price": _safe_number(latest.get("close"), 0),
        "vwap": _safe_number(latest.get("vwap"), latest.get("close")),
        "ema9": ema9,
        "ema21": ema21,
        "adx": _safe_number(latest.get("adx"), 0),
        "volume_ratio": _safe_number(latest.get("volume_ratio"), 0),
        "vwap_fallback_used": False,
        "supertrend_dir": str(latest.get("supertrend_dir") or "NEUTRAL"),
        "trend": trend,
        "mtf_confirmed": trend != "SIDEWAYS",
        "c1_bullish": _safe_number(previous.get("close")) > _safe_number(previous.get("open")),
        "c2_bullish": _safe_number(latest.get("close")) > _safe_number(latest.get("open")),
        "gap_day": False,
        "orb_high": 0.0,
        "orb_low": 0.0,
        "atr": _safe_number(latest.get("atr"), 0),
        "signal": final_signal,
        "signal_score": signal_data["score"],
        "signal_min_score": signal_data["min_score"],
    }

    return {
        "underlying": underlying,
        "status": "OK",
        "market_data": market_data,
        "signal_data": signal_data,
        "chart_candles": chart_candles[-390:],
        "candle_id": str(latest.get("time")),
        "candle_count": len(frame),
        "data_source": source or "REPLAY_ALIGNED",
        "data_note": " | ".join(notes)[:500],
        "replay_aligned": True,
    }


def apply_live_scan_history_fallback_patch():
    if getattr(runtime, "_okai_live_history_fallback_v4", False):
        return

    original_summary = runtime._summary

    def patched_summary(scan):
        summary = original_summary(scan)
        summary["candle_count"] = int(scan.get("candle_count") or 0)
        summary["data_source"] = scan.get("data_source")
        summary["data_note"] = scan.get("data_note")
        summary["replay_aligned"] = bool(scan.get("replay_aligned", False))
        return summary

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
                    "candidate_signal": f"{candle_count} CANDLES",
                    "score": 0,
                    "min_score": int(profile.get("entry_threshold", 82) or 82),
                    "trade_allowed": False,
                    "candle_count": candle_count,
                    "data_source": source or "NONE",
                    "error": " | ".join(note for note in notes if note)[:500]
                    or f"Only {candle_count} usable candles",
                })
                continue

            _remember_frame(user_id, underlying, frame)

            try:
                scan = runtime._build_scan(
                    user_id,
                    underlying,
                    frame,
                    profile,
                    streak,
                )
                if scan.get("status") != "OK":
                    raise RuntimeError(f"legacy build status={scan.get('status')}")
                scan["candle_count"] = candle_count
                scan["data_source"] = source or "LIVE_BROKER"
                if notes:
                    scan["data_note"] = " | ".join(notes)[:500]
                scans.append(scan)
            except Exception as exc:
                notes.append("legacy_build_error=" + str(exc)[:180])
                try:
                    scans.append(
                        _replay_aligned_scan(
                            user_id,
                            underlying,
                            frame,
                            profile,
                            source,
                            notes,
                        )
                    )
                except Exception as replay_exc:
                    scans.append({
                        "underlying": underlying,
                        "status": "ERROR",
                        "signal": "WAIT",
                        "candidate_signal": "REPLAY ERROR",
                        "score": 0,
                        "min_score": int(profile.get("entry_threshold", 82) or 82),
                        "trade_allowed": False,
                        "candle_count": candle_count,
                        "data_source": source or "UNKNOWN",
                        "error": (
                            " | ".join(notes)
                            + " | replay_error="
                            + str(replay_exc)[:180]
                        )[:500],
                    })

        return scans

    runtime._summary = patched_summary
    runtime._scan_multi = patched_scan_multi
    runtime._okai_live_history_fallback_v4 = True
