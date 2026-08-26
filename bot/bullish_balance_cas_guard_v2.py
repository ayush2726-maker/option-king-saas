"""Minimal balanced momentum and SEBI CAS safety for AUTO runtime.

- Neutral/doji candles never count as PE momentum.
- A trend-aligned pullback/reclaim is accepted symmetrically for CE and PE.
- Fresh AUTO entries stop at 15:15 IST and open positions force-exit at 15:25 IST.
- The entry lock resets automatically at the next trading session.

The protected score threshold, MTF, ADX, anti-chase, sizing, cooldown, broker
orders, ATR SL and profit-lock logic are not changed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime


PATCH_VERSION = "ENTRY_1515_FORCE_EXIT_1525_V3"
FRESH_ENTRY_CUTOFF_MINUTE = 15 * 60 + 15
FORCE_EXIT_MINUTE = 15 * 60 + 25
FRESH_ENTRY_BLOCK_REASON = "FRESH_ENTRY_CUTOFF_15_15_IST"
CAS_EFFECTIVE_DATE = date(2026, 8, 3)
CAS_SAFE_EXIT_MINUTE = FORCE_EXIT_MINUTE
CAS_START_MINUTE = 15 * 60 + 15
CAS_END_MINUTE = 15 * 60 + 35
DERIVATIVES_CLOSE_MINUTE = 15 * 60 + 40
LEGACY_EOD_EXIT_MINUTE = 15 * 60 + 25


def _f(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _value(row, key, default=None):
    try:
        value = row.get(key, default)
    except Exception:
        try:
            value = row[key]
        except Exception:
            value = default
    return default if value is None else value


def _now_ist():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _minute(value):
    return value.hour * 60 + value.minute


def _cas_day(value=None):
    current = value or _now_ist()
    return current.weekday() < 5 and current.date() >= CAS_EFFECTIVE_DATE


def eod_exit_minute_for(value=None):
    return FORCE_EXIT_MINUTE if _cas_day(value) else LEGACY_EOD_EXIT_MINUTE


def fresh_entry_blocked(value=None):
    current = value or _now_ist()
    return _cas_day(current) and _minute(current) >= FRESH_ENTRY_CUTOFF_MINUTE


def classify_completed_candle(row, atr=0.0):
    open_price = _f(_value(row, "open"), 0.0)
    high = _f(_value(row, "high"), open_price)
    low = _f(_value(row, "low"), open_price)
    close = _f(_value(row, "close"), open_price)
    body = close - open_price
    candle_range = max(0.01, high - low)
    threshold = max(0.5, candle_range * 0.15, max(0.0, _f(atr)) * 0.04)

    if body > threshold:
        direction = "UP"
    elif body < -threshold:
        direction = "DOWN"
    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "body_points": round(body, 4),
        "range_points": round(candle_range, 4),
        "neutral_threshold": round(threshold, 4),
        "open": open_price,
        "close": close,
    }


def momentum_pattern(first, second, market):
    first_direction = str(first.get("direction") or "NEUTRAL").upper()
    second_direction = str(second.get("direction") or "NEUTRAL").upper()

    if first_direction == "UP" and second_direction == "UP":
        return "TWO_CLEAR_BULLISH"
    if first_direction == "DOWN" and second_direction == "DOWN":
        return "TWO_CLEAR_BEARISH"

    trend = str(market.get("trend") or "SIDEWAYS").upper()
    supertrend = str(market.get("supertrend_dir") or "NEUTRAL").upper()
    close = _f(second.get("close"), _f(market.get("price"), 0.0))
    ema9 = _f(market.get("ema9"), close)
    vwap = _f(market.get("vwap"), close)
    vwap_fallback = bool(market.get("vwap_fallback_used", False))

    if (
        first_direction in {"DOWN", "NEUTRAL"}
        and second_direction == "UP"
        and trend == "UPTREND"
        and supertrend == "UP"
        and close > ema9
        and (vwap_fallback or close > vwap)
    ):
        return "BULLISH_PULLBACK_RECLAIM"

    if (
        first_direction in {"UP", "NEUTRAL"}
        and second_direction == "DOWN"
        and trend == "DOWNTREND"
        and supertrend == "DOWN"
        and close < ema9
        and (vwap_fallback or close < vwap)
    ):
        return "BEARISH_PULLBACK_REJECTION"

    return "NO_MOMENTUM"


def momentum_score_flags(pattern):
    value = str(pattern or "NO_MOMENTUM").upper()
    if value in {"TWO_CLEAR_BULLISH", "BULLISH_PULLBACK_RECLAIM"}:
        return True, True
    if value in {"TWO_CLEAR_BEARISH", "BEARISH_PULLBACK_REJECTION"}:
        return False, False
    # Mixed flags award neither CE nor PE momentum in the legacy interface.
    return True, False


def apply_balanced_momentum_patch():
    if getattr(runtime, "_okai_balanced_momentum_v2", False):
        return

    original_build_scan = runtime._build_scan

    def build_scan_balanced(user_id, underlying, frame, profile, loss_streak):
        scan = original_build_scan(
            user_id,
            underlying,
            frame,
            profile,
            loss_streak,
        )
        if not isinstance(scan, dict) or scan.get("status") != "OK":
            return scan

        try:
            if frame is None or len(frame) < 3:
                return scan

            market = dict(scan.get("market_data") or {})
            first_row = frame.iloc[-3]
            second_row = frame.iloc[-2]
            atr = _f(market.get("atr"), _f(_value(second_row, "ATR"), 0.0))
            first = classify_completed_candle(first_row, atr)
            second = classify_completed_candle(second_row, atr)
            pattern = momentum_pattern(first, second, market)
            score_c1_bullish, score_c2_bullish = momentum_score_flags(pattern)

            # Keep explicit real directions, while the two legacy booleans carry
            # only the balanced momentum result used by existing score code.
            market.update(
                {
                    "c1_direction": first["direction"],
                    "c2_direction": second["direction"],
                    "c1_bullish_actual": first["direction"] == "UP",
                    "c2_bullish_actual": second["direction"] == "UP",
                    "c1_bearish_actual": first["direction"] == "DOWN",
                    "c2_bearish_actual": second["direction"] == "DOWN",
                    "c1_neutral": first["direction"] == "NEUTRAL",
                    "c2_neutral": second["direction"] == "NEUTRAL",
                    "c1_bullish": score_c1_bullish,
                    "c2_bullish": score_c2_bullish,
                    "c1_body_points": first["body_points"],
                    "c2_body_points": second["body_points"],
                    "candle_neutral_threshold": max(
                        first["neutral_threshold"],
                        second["neutral_threshold"],
                    ),
                    "momentum_pattern": pattern,
                    "momentum_balance_version": PATCH_VERSION,
                }
            )

            fixed_signal = angel_fetcher.get_full_signal(
                market,
                consecutive_losses=loss_streak,
                profile=profile,
            )
            if not isinstance(fixed_signal, dict):
                return scan

            previous_signal = dict(scan.get("signal_data") or {})
            fixed_signal = dict(fixed_signal)
            for key, value in previous_signal.items():
                fixed_signal.setdefault(key, value)

            fixed_signal.update(
                {
                    "momentum_pattern": pattern,
                    "c1_direction": first["direction"],
                    "c2_direction": second["direction"],
                    "neutral_candle_pe_bias_removed": True,
                }
            )
            warnings = list(fixed_signal.get("warnings") or [])
            if (
                pattern == "NO_MOMENTUM"
                and "NEUTRAL" in {first["direction"], second["direction"]}
                and "NEUTRAL_CANDLE_NOT_COUNTED_AS_PE" not in warnings
            ):
                warnings.append("NEUTRAL_CANDLE_NOT_COUNTED_AS_PE")
            if pattern == "BULLISH_PULLBACK_RECLAIM":
                warnings.append("BULLISH_PULLBACK_RECLAIM_CONFIRMED")
            elif pattern == "BEARISH_PULLBACK_REJECTION":
                warnings.append("BEARISH_PULLBACK_REJECTION_CONFIRMED")
            fixed_signal["warnings"] = list(dict.fromkeys(warnings))

            market["signal"] = fixed_signal.get("signal", "WAIT")
            market["signal_score"] = fixed_signal.get("score", 0)
            market["signal_min_score"] = fixed_signal.get("min_score", 82)

            fixed_scan = dict(scan)
            fixed_scan["market_data"] = market
            fixed_scan["signal_data"] = fixed_signal
            return fixed_scan
        except Exception as exc:
            scan.setdefault("momentum_balance_warning", str(exc)[:160])
            return scan

    runtime._build_scan = build_scan_balanced
    runtime._okai_balanced_momentum_v2 = True


def apply_cas_closing_guard_patch():
    if getattr(runtime, "_okai_entry_1515_force_exit_1525_v3", False):
        return

    original_evaluate_exit = runtime._evaluate_exit
    original_state_update = runtime._state_update
    original_open_common = runtime._open_common
    original_live_gateway_entry = getattr(
        angel_fetcher,
        "_manage_live_gateway_entry",
        None,
    )
    original_queue_live_entry = getattr(angel_fetcher, "queue_live_entry", None)

    def evaluate_exit_cas_safe(trade, ltp, market_data, candle_id):
        result = original_evaluate_exit(trade, ltp, market_data, candle_id)
        if not isinstance(result, dict):
            return result

        current = _now_ist()
        if (
            _cas_day(current)
            and _minute(current) >= FORCE_EXIT_MINUTE
            and not result.get("reason")
        ):
            result = dict(result)
            actual_exit_ist = current.strftime("%H:%M")
            result["reason"] = f"FORCE EXIT {actual_exit_ist} IST"
            result["cas_guard"] = {
                "version": PATCH_VERSION,
                "fresh_entry_cutoff_ist": "15:15",
                "force_exit_ist": "15:25",
                "actual_exit_ist": actual_exit_ist,
                "cas_window_ist": "15:15-15:35",
                "derivatives_close_ist": "15:40",
            }
        return result

    def open_common_with_1515_cutoff(
        conn,
        user_id,
        broker_name,
        selected,
        settings,
        resolved,
        quote_price,
        quality,
        lot_size,
        live_order,
        live_cash,
        state,
    ):
        current = _now_ist()
        if fresh_entry_blocked(current):
            details = {
                "fresh_entry_cutoff": "15:15",
                "force_exit": "15:25",
                "version": PATCH_VERSION,
                "blocked_at_ist": current.strftime("%H:%M"),
            }
            try:
                return runtime._record_preopen_failure(
                    state,
                    broker_name,
                    selected,
                    FRESH_ENTRY_BLOCK_REASON,
                    "TIME_GUARD",
                    details,
                )
            except Exception:
                attempt = {
                    "allowed": False,
                    "reason": FRESH_ENTRY_BLOCK_REASON,
                    "stage": "TIME_GUARD",
                    **details,
                }
                state["entry_guard"] = dict(attempt)
                state["entry_attempt"] = dict(attempt)
                state["last_entry_attempt"] = dict(attempt)
                state["entry_block_reason"] = FRESH_ENTRY_BLOCK_REASON
                state["last_entry_block_reason"] = FRESH_ENTRY_BLOCK_REASON
                return False

        return original_open_common(
            conn,
            user_id,
            broker_name,
            selected,
            settings,
            resolved,
            quote_price,
            quality,
            lot_size,
            live_order,
            live_cash,
            state,
        )

    def live_gateway_entry_with_1515_cutoff(*args, **kwargs):
        current = _now_ist()
        if fresh_entry_blocked(current):
            return {
                "queued": False,
                "reason": FRESH_ENTRY_BLOCK_REASON,
                "fresh_entry_cutoff": "15:15",
                "force_exit": "15:25",
                "version": PATCH_VERSION,
                "blocked_at_ist": current.strftime("%H:%M"),
            }
        return original_live_gateway_entry(*args, **kwargs)

    def queue_live_entry_with_1525_exit(user_id, payload, *args, **kwargs):
        safe_payload = dict(payload or {})
        safe_payload["fresh_entry_cutoff"] = "15:15"
        safe_payload["force_exit_at"] = "15:25"
        return original_queue_live_entry(
            user_id,
            safe_payload,
            *args,
            **kwargs,
        )

    def state_update_cas_safe(state, scans, selected, settings, rows):
        original_state_update(state, scans, selected, settings, rows)
        if _cas_day():
            state.update(
                {
                    "closing_system": "SEBI_CLOSING_AUCTION_SESSION",
                    "cas_guard_active": True,
                    "cas_effective_date": CAS_EFFECTIVE_DATE.isoformat(),
                    "cas_window_ist": "15:15-15:35",
                    "derivatives_close_ist": "15:40",
                    "hard_eod_exit_ist": "15:25",
                    "fresh_entry_cutoff_ist": "15:15",
                    "force_exit_ist": "15:25",
                    "fresh_entry_locked": fresh_entry_blocked(),
                    "cas_feed_mode": "NO_INDICATIVE_AUCTION_FEED_PRE_CLOSE_EXIT",
                }
            )

    runtime._evaluate_exit = evaluate_exit_cas_safe
    runtime._open_common = open_common_with_1515_cutoff
    runtime._state_update = state_update_cas_safe
    if callable(original_live_gateway_entry):
        angel_fetcher._manage_live_gateway_entry = live_gateway_entry_with_1515_cutoff
    if callable(original_queue_live_entry):
        angel_fetcher.queue_live_entry = queue_live_entry_with_1525_exit
    runtime._okai_cas_closing_guard_v2 = True
    runtime._okai_entry_1515_force_exit_1525_v3 = True
