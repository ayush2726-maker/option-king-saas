"""Provide indicator warmup history for the early-session Live Strategy Score.

The normal 1-day history request intentionally starts at today's 09:15 IST.
The replay scorer needs 28 candles, so before ~09:43 the app can otherwise show
0/100 with ADX 0.0 and Volume 0.00x even though the broker feed is healthy.

This patch keeps the public 1-day chart unchanged.  Only the live AUTO scan gets
up to five calendar days of 1-minute candles for EMA/ADX/volume warmup.  The
historical replay code already resets session-sensitive VWAP, ORB and
Supertrend by trading day.

Trading safety is preserved: while today's session still has fewer than the
existing 28-candle requirement, the calculated score is display-only and the
signal is forced to WAIT.  Once 28 current-session candles exist, the original
trade decision is left unchanged.
"""

from datetime import datetime, timezone, timedelta

from bot import history_provider
from bot import live_scan_history_fallback_patch as live_scan


WARMUP_LOOKBACK_DAYS = 5
SESSION_MIN_CANDLES = int(getattr(live_scan, "MIN_CANDLES", 28) or 28)


def _today_session_count(frame) -> int:
    if frame is None or getattr(frame, "empty", True):
        return 0
    try:
        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        today = now_ist.strftime("%Y-%m-%d")
        times = frame["time"]
        try:
            local = times.dt.tz_convert("Asia/Kolkata")
        except Exception:
            import pandas as pd

            local = pd.to_datetime(times, errors="coerce", utc=True).dt.tz_convert(
                "Asia/Kolkata"
            )
        return int((local.dt.strftime("%Y-%m-%d") == today).sum())
    except Exception:
        return 0


def apply_early_live_score_warmup_patch() -> None:
    if getattr(live_scan, "_okai_early_live_score_warmup_v1", False):
        return

    original_request_arguments = history_provider._request_arguments
    original_provider_frame = live_scan._provider_frame
    original_replay_scan = live_scan._replay_scan

    def request_arguments_with_intraday_warmup(broker_name, broker, instrument, days):
        args = original_request_arguments(broker_name, broker, instrument, days)
        if int(days or 1) != WARMUP_LOOKBACK_DAYS:
            return args

        # Keep the original date boundaries/exchange resolution, but request
        # one-minute candles for this dedicated live-score warmup window.
        values = list(args)
        if len(values) >= 2:
            values[1] = "1m"
        return tuple(values)

    def provider_frame_with_warmup(user_id, underlying):
        frame, source, note = original_provider_frame(user_id, underlying)
        if frame is not None and len(frame) >= SESSION_MIN_CANDLES:
            return frame, source, note

        notes = [str(note or "").strip()]
        try:
            rows, reason, broker_name, cached = history_provider.get_historical_rows(
                user_id,
                underlying,
                WARMUP_LOOKBACK_DAYS,
            )
            warmup = live_scan._frame_from_rows(rows)
            notes.append(
                "warmup="
                f"{broker_name or 'none'} cached={bool(cached)} rows={len(rows or [])}"
            )
            if reason:
                notes.append(str(reason)[:120])
            if warmup is not None and len(warmup) >= SESSION_MIN_CANDLES:
                return (
                    warmup,
                    "HISTORY_PROVIDER_WARMUP_1M",
                    " | ".join(value for value in notes if value)[:500],
                )
        except Exception as exc:
            notes.append("warmup_error=" + str(exc)[:150])

        return frame, source, " | ".join(value for value in notes if value)[:500]

    def replay_scan_with_session_guard(user_id, underlying, frame, profile, source, notes):
        scan = original_replay_scan(
            user_id,
            underlying,
            frame,
            profile,
            source,
            notes,
        )
        if not isinstance(scan, dict):
            return scan

        session_count = _today_session_count(frame)
        scan["candle_count"] = session_count
        scan["warmup_candle_count"] = len(frame) if frame is not None else 0

        signal = scan.get("signal_data")
        market = scan.get("market_data")
        if session_count < SESSION_MIN_CANDLES and isinstance(signal, dict):
            # Score/indicator values remain visible, but entry behaviour stays
            # exactly as strict as before this display fix.
            signal["trade_allowed"] = False
            signal["signal"] = "WAIT"
            warnings = list(signal.get("warnings") or [])
            warnings.append(
                f"SESSION_WARMUP_{session_count}_OF_{SESSION_MIN_CANDLES}_DISPLAY_ONLY"
            )
            signal["warnings"] = list(dict.fromkeys(warnings))
            signal["early_session_display_only"] = True
            signal["session_candle_count"] = session_count
            signal["session_min_candles"] = SESSION_MIN_CANDLES
            if isinstance(market, dict):
                market["signal"] = "WAIT"
            scan["data_note"] = (
                str(scan.get("data_note") or "")
                + f" | score warmup uses prior session; entry waits for {SESSION_MIN_CANDLES} today candles"
            )[:500]

        return scan

    history_provider._request_arguments = request_arguments_with_intraday_warmup
    live_scan._provider_frame = provider_frame_with_warmup
    live_scan._replay_scan = replay_scan_with_session_guard
    live_scan._okai_early_live_score_warmup_v1 = True
