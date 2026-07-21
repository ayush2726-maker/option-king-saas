"""Replay-first AUTO scan recovery.

The graph endpoint already computes strategy scores reliably from broker candles.
This patch makes the AUTO portfolio runtime use that same calculation directly
for NIFTY, BANKNIFTY and SENSEX instead of depending on the older live builder.
It also returns useful diagnostics in the existing mobile UI and falls back to a
recent saved score for display only when candle calculation is temporarily down.
"""

from datetime import datetime, timedelta, timezone
import math
import time

from bot import auto_portfolio_runtime as runtime
from bot import history_provider
from database import get_db


MIN_CANDLES = 28
LAST_GOOD_SECONDS = 300
SAVED_SCORE_MAX_AGE_SECONDS = 300
_LAST_GOOD_FRAMES = {}


def _safe_number(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except Exception:
        try:
            return float(default)
        except Exception:
            return 0.0


def _safe_int(value, default=0):
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _profile_dict(profile):
    if isinstance(profile, dict):
        result = dict(profile)
    else:
        result = {}

    result.setdefault("profile_key", "okai_default_82")
    result.setdefault("profile_name", "OKAI Default 82")
    result.setdefault("entry_threshold", 82)
    return result


def _copy_frame(frame):
    if frame is None:
        return None
    try:
        return frame.copy()
    except Exception:
        return frame


def _frame_from_rows(rows):
    import pandas as pd

    normalised = []
    for row in rows or []:
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

    for column in ("open", "high", "low", "close", "volume"):
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
            prices = [float(row[name]) for name in ("open", "high", "low", "close")]
            if all(math.isfinite(price) and price > 0 for price in prices):
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
        timestamp = row.get("time")
        try:
            timestamp = timestamp.isoformat()
        except Exception:
            timestamp = str(timestamp)
        rows.append([
            timestamp,
            row.get("open"),
            row.get("high"),
            row.get("low"),
            row.get("close"),
            row.get("volume", 0),
        ])
    return rows


def _remember_frame(user_id, underlying, frame):
    if frame is None or len(frame) < MIN_CANDLES:
        return
    _LAST_GOOD_FRAMES[(int(user_id), str(underlying).upper())] = (
        time.monotonic(),
        _copy_frame(frame),
    )


def _recent_frame(user_id, underlying):
    item = _LAST_GOOD_FRAMES.get((int(user_id), str(underlying).upper()))
    if not item:
        return None
    saved_at, frame = item
    if time.monotonic() - saved_at > LAST_GOOD_SECONDS:
        return None
    return _copy_frame(frame)


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
        notes.append("provider_error=" + str(exc)[:150])

    try:
        cache_key = (int(user_id), str(underlying).upper(), 1)
        cached_item = getattr(history_provider, "_CACHE", {}).get(cache_key) or {}
        rows = cached_item.get("rows") or []
        frame = _frame_from_rows(rows)
        notes.append(f"direct_cache_rows={len(rows)}")
        if frame is not None and len(frame) >= MIN_CANDLES:
            return frame, "CHART_CACHE_DIRECT", " | ".join(notes)
    except Exception as exc:
        notes.append("cache_error=" + str(exc)[:130])

    return None, None, " | ".join(notes)


def _upstox_range_frame(broker_obj, underlying):
    try:
        from bot.angel_fetcher import UPSTOX_INDEX_KEYS

        key = UPSTOX_INDEX_KEYS[str(underlying).upper()]
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
            return None, str((result or {}).get("message") or "range request failed")[:180]

        rows = result.get("candles") or []
        frame = _frame_from_rows(rows)
        if frame is None:
            return None, f"range rows unusable={len(rows)}"

        today_rows = frame[
            frame["time"].dt.tz_convert("Asia/Kolkata").dt.strftime("%Y-%m-%d")
            == today
        ].reset_index(drop=True)
        if len(today_rows) >= MIN_CANDLES:
            frame = today_rows
        return frame, f"range_rows={len(frame)}"
    except Exception as exc:
        return None, str(exc)[:180]


def _latest_saved_score(user_id, underlying):
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT score, price, signal, adx, volume_ratio, created_at
            FROM signal_history
            WHERE user_id=? AND instrument=?
              AND score IS NOT NULL AND CAST(score AS REAL) > 0
              AND price IS NOT NULL AND CAST(price AS REAL) > 0
            ORDER BY id DESC LIMIT 1
            """,
            (int(user_id), str(underlying).upper()),
        ).fetchone()
        if not row:
            return None

        created_text = str(row["created_at"] or "")
        try:
            created = datetime.fromisoformat(created_text.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
            if age > SAVED_SCORE_MAX_AGE_SECONDS:
                return None
        except Exception:
            return None

        return {
            "score": _safe_int(row["score"], 0),
            "price": _safe_number(row["price"], 0),
            "signal": str(row["signal"] or "WAIT").upper(),
            "adx": _safe_number(row["adx"], 0),
            "volume_ratio": _safe_number(row["volume_ratio"], 0),
            "created_at": created_text,
        }
    finally:
        conn.close()


def _saved_display_scan(user_id, underlying, profile, notes):
    saved = _latest_saved_score(user_id, underlying)
    if not saved:
        return None

    candidate = saved["signal"] if saved["signal"] in ("CE", "PE") else "WAIT"
    signal_data = {
        "signal": "WAIT",
        "candidate_signal": candidate,
        "score": saved["score"],
        "min_score": _safe_int(profile.get("entry_threshold"), 82),
        "trade_allowed": False,
        "adx": saved["adx"],
        "volume_ratio": saved["volume_ratio"],
        "warnings": ["RECENT_SAVED_SCORE_DISPLAY_ONLY"],
        "strategy": "CUSTOM_PROFILE_V1" if profile.get("profile_key") != "okai_default_82" else "TQU_ENHANCED",
        "strategy_profile_key": profile.get("profile_key"),
        "strategy_profile_name": profile.get("profile_name"),
    }
    return {
        "underlying": underlying,
        "status": "RECENT_SCORE",
        "market_data": {
            "price": saved["price"],
            "adx": saved["adx"],
            "volume_ratio": saved["volume_ratio"],
        },
        "signal_data": signal_data,
        "chart_candles": [],
        "candle_id": saved["created_at"],
        "candle_count": 0,
        "data_source": "SIGNAL_HISTORY_RECENT",
        "data_note": " | ".join(notes)[:500],
        "replay_aligned": False,
    }


def _replay_scan(user_id, underlying, frame, profile, source, notes):
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
        raise RuntimeError("NO_REPLAY_SCORE")

    # The latest broker candle can still be forming, so use the previous scored
    # candle when available.
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
        "score": _safe_int(latest.get("score"), 0),
        "base_score": _safe_int(latest.get("score"), 0),
        "min_score": _safe_int(latest.get("min_score"), profile.get("entry_threshold", 82)),
        "trade_allowed": allowed,
        "adx": _safe_number(latest.get("adx"), 0),
        "volume_ratio": _safe_number(latest.get("volume_ratio"), 0),
        "mtf_confirmed": trend != "SIDEWAYS",
        "warnings": ["REPLAY_FIRST_LIVE_SCAN"],
        "strategy": "CUSTOM_PROFILE_V1" if profile.get("profile_key") != "okai_default_82" else "TQU_ENHANCED",
        "strategy_profile_key": profile.get("profile_key"),
        "strategy_profile_name": profile.get("profile_name"),
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
        "data_source": source or "REPLAY_FIRST",
        "data_note": " | ".join(notes)[:500],
        "replay_aligned": True,
    }


def _failure_scan(underlying, profile, status, candle_count, source, notes, code):
    threshold = _safe_int(profile.get("entry_threshold"), 82)
    return {
        "underlying": underlying,
        "status": status,
        "market_data": {},
        "signal_data": {
            "signal": "WAIT",
            "candidate_signal": code[:32],
            "score": 0,
            "min_score": threshold,
            "trade_allowed": False,
            "warnings": [code[:80]],
        },
        "candle_count": int(candle_count or 0),
        "data_source": source or "NONE",
        "data_note": " | ".join(notes)[:500],
        "error": " | ".join(notes)[:500],
    }


def _collect_frame(user_id, broker_name, broker_obj, underlying, angel=False):
    frame = None
    source = None
    notes = []

    try:
        if angel:
            legacy = runtime._legacy()
            frame = legacy.get_candles(
                broker_obj,
                legacy.INDEX_TOKENS[underlying],
                exchange=legacy.INDEX_EXCHANGE[underlying],
            )
        else:
            frame = runtime._legacy().get_candles_multi(
                broker_name,
                broker_obj,
                underlying,
            )
        notes.append(f"direct_rows={len(frame) if frame is not None else 0}")
        if frame is not None and len(frame) >= MIN_CANDLES:
            source = "LIVE_BROKER"
    except Exception as exc:
        notes.append("direct_error=" + str(exc)[:170])

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
        and not angel
        and str(broker_name).lower() == "upstox"
    ):
        range_frame, range_note = _upstox_range_frame(broker_obj, underlying)
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

    return frame, source, notes


def _scan_all(user_id, broker_name, broker_obj, settings, profile, angel=False):
    profile = _profile_dict(profile)
    scans = []

    for underlying in runtime._enabled(settings):
        frame, source, notes = _collect_frame(
            user_id,
            broker_name,
            broker_obj,
            underlying,
            angel=angel,
        )
        candle_count = len(frame) if frame is not None else 0

        if frame is None or candle_count < MIN_CANDLES:
            saved_scan = _saved_display_scan(user_id, underlying, profile, notes)
            if saved_scan:
                scans.append(saved_scan)
            else:
                scans.append(
                    _failure_scan(
                        underlying,
                        profile,
                        "WAITING_CANDLES",
                        candle_count,
                        source,
                        notes,
                        f"{candle_count} CANDLES",
                    )
                )
            continue

        _remember_frame(user_id, underlying, frame)

        try:
            scans.append(
                _replay_scan(
                    user_id,
                    underlying,
                    frame,
                    profile,
                    source,
                    notes,
                )
            )
        except Exception as exc:
            notes.append("replay_error=" + str(exc)[:180])
            saved_scan = _saved_display_scan(user_id, underlying, profile, notes)
            if saved_scan:
                scans.append(saved_scan)
            else:
                scans.append(
                    _failure_scan(
                        underlying,
                        profile,
                        "ERROR",
                        candle_count,
                        source,
                        notes,
                        "REPLAY ERROR",
                    )
                )

    return scans


def apply_live_scan_history_fallback_patch():
    if getattr(runtime, "_okai_replay_first_scan_v5", False):
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
        return _scan_all(
            user_id,
            broker_name,
            obj,
            settings,
            profile,
            angel=False,
        )

    def patched_scan_angel(user_id, obj, settings, profile, streak):
        return _scan_all(
            user_id,
            "angelone",
            obj,
            settings,
            profile,
            angel=True,
        )

    runtime._summary = patched_summary
    runtime._scan_multi = patched_scan_multi
    runtime._scan_angel = patched_scan_angel
    runtime._okai_replay_first_scan_v5 = True
