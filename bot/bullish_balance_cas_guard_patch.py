"""Balanced CE/PE momentum and Closing Auction Session safety.

This patch fixes two independent issues without changing the protected score
threshold, position sizing, ATR risk, broker routing, cooldown, or MTF rules.

1. Candle momentum balance
   The legacy PE momentum check used ``not bullish`` for both candles, so a doji
   or flat candle could count as bearish.  We classify completed candles as UP,
   DOWN or NEUTRAL and award the momentum component only for:
   - two clear candles in the same direction; or
   - a symmetric trend pullback/reclaim pattern.

2. SEBI Closing Auction Session (CAS)
   CAS applies from 03-Aug-2026.  The cash-market transition begins at 15:15 IST
   while equity derivatives remain open later.  Until the engine consumes an
   official real-time indicative auction-price feed, open AUTO positions are
   closed at 15:12 IST, leaving a three-minute broker execution buffer.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime
from bot import routes
from bot import strategy


PATCH_VERSION = "BULLISH_BALANCE_CAS_GUARD_V1"
CAS_EFFECTIVE_DATE = date(2026, 8, 3)
CAS_SAFE_EXIT_MINUTE = 15 * 60 + 12
CAS_START_MINUTE = 15 * 60 + 15
CAS_END_MINUTE = 15 * 60 + 35
DERIVATIVES_CLOSE_MINUTE = 15 * 60 + 40
LEGACY_EOD_EXIT_MINUTE = 15 * 60 + 25


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row.get(key, default)
    except Exception:
        try:
            value = row[key]
        except Exception:
            value = default
    return default if value is None else value


def _now_ist() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _minute_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _cas_day(value: datetime | None = None) -> bool:
    current = value or _now_ist()
    return current.weekday() < 5 and current.date() >= CAS_EFFECTIVE_DATE


def eod_exit_minute_for(value: datetime | None = None) -> int:
    return CAS_SAFE_EXIT_MINUTE if _cas_day(value) else LEGACY_EOD_EXIT_MINUTE


def classify_completed_candle(row: Any, atr: float = 0.0) -> dict[str, Any]:
    """Classify one completed candle while keeping small bodies neutral.

    The threshold adapts to both the candle range and current 1-minute ATR.  It
    prevents zero/small bodies from being treated as PE momentum while remaining
    usable across NIFTY, BANKNIFTY and SENSEX point scales.
    """

    open_price = _f(_row_value(row, "open"), 0.0)
    high = _f(_row_value(row, "high"), open_price)
    low = _f(_row_value(row, "low"), open_price)
    close = _f(_row_value(row, "close"), open_price)
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


def momentum_pattern(
    first: dict[str, Any],
    second: dict[str, Any],
    market: dict[str, Any],
) -> str:
    """Return one symmetric momentum pattern for CE/PE scoring."""

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

    bullish_reclaim = (
        first_direction in {"DOWN", "NEUTRAL"}
        and second_direction == "UP"
        and trend == "UPTREND"
        and supertrend == "UP"
        and close > ema9
        and (vwap_fallback or close > vwap)
    )
    if bullish_reclaim:
        return "BULLISH_PULLBACK_RECLAIM"

    bearish_rejection = (
        first_direction in {"UP", "NEUTRAL"}
        and second_direction == "DOWN"
        and trend == "DOWNTREND"
        and supertrend == "DOWN"
        and close < ema9
        and (vwap_fallback or close < vwap)
    )
    if bearish_rejection:
        return "BEARISH_PULLBACK_REJECTION"

    return "NO_MOMENTUM"


def momentum_score_flags(pattern: str) -> tuple[bool, bool]:
    """Map the balanced pattern into the legacy two-boolean score interface.

    A mixed True/False pair intentionally awards neither CE nor PE momentum.
    """

    value = str(pattern or "NO_MOMENTUM").upper()
    if value in {"TWO_CLEAR_BULLISH", "BULLISH_PULLBACK_RECLAIM"}:
        return True, True
    if value in {"TWO_CLEAR_BEARISH", "BEARISH_PULLBACK_REJECTION"}:
        return False, False
    return True, False


def apply_balanced_momentum_patch() -> None:
    """Install neutral-candle and pullback-reclaim scoring before final guards."""

    if getattr(runtime, "_okai_balanced_momentum_v1", False):
        return

    original_build_scan = runtime._build_scan
    original_get_full_signal = angel_fetcher.get_full_signal

    def balanced_get_full_signal(
        market_data: dict,
        consecutive_losses: int = 0,
        profile: dict | None = None,
    ) -> dict:
        score_market = dict(market_data or {})
        pattern = score_market.get("momentum_pattern")

        if pattern:
            first_bullish, second_bullish = momentum_score_flags(str(pattern))
            score_market["c1_bullish"] = first_bullish
            score_market["c2_bullish"] = second_bullish

        result = original_get_full_signal(
            score_market,
            consecutive_losses=consecutive_losses,
            profile=profile,
        )
        if not isinstance(result, dict):
            return result

        fixed = dict(result)
        if pattern:
            fixed["momentum_pattern"] = str(pattern)
            fixed["c1_direction"] = score_market.get("c1_direction", "NEUTRAL")
            fixed["c2_direction"] = score_market.get("c2_direction", "NEUTRAL")
            fixed["neutral_candle_pe_bias_removed"] = True

            warnings = list(fixed.get("warnings") or [])
            if (
                "NEUTRAL" in {
                    str(score_market.get("c1_direction") or "").upper(),
                    str(score_market.get("c2_direction") or "").upper(),
                }
                and str(pattern) == "NO_MOMENTUM"
                and "NEUTRAL_CANDLE_NOT_COUNTED_AS_PE" not in warnings
            ):
                warnings.append("NEUTRAL_CANDLE_NOT_COUNTED_AS_PE")
            if str(pattern) == "BULLISH_PULLBACK_RECLAIM":
                if "BULLISH_PULLBACK_RECLAIM_CONFIRMED" not in warnings:
                    warnings.append("BULLISH_PULLBACK_RECLAIM_CONFIRMED")
            elif str(pattern) == "BEARISH_PULLBACK_REJECTION":
                if "BEARISH_PULLBACK_REJECTION_CONFIRMED" not in warnings:
                    warnings.append("BEARISH_PULLBACK_REJECTION_CONFIRMED")
            fixed["warnings"] = warnings

        return fixed

    def build_scan_with_balanced_momentum(
        user_id,
        underlying,
        frame,
        profile,
        loss_streak,
    ):
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
            atr = _f(market.get("atr"), _f(_row_value(second_row, "ATR"), 0.0))
            first = classify_completed_candle(first_row, atr)
            second = classify_completed_candle(second_row, atr)
            pattern = momentum_pattern(first, second, market)

            market.update(
                {
                    "c1_direction": first["direction"],
                    "c2_direction": second["direction"],
                    "c1_bullish": first["direction"] == "UP",
                    "c2_bullish": second["direction"] == "UP",
                    "c1_bearish": first["direction"] == "DOWN",
                    "c2_bearish": second["direction"] == "DOWN",
                    "c1_neutral": first["direction"] == "NEUTRAL",
                    "c2_neutral": second["direction"] == "NEUTRAL",
                    "c1_body_points": first["body_points"],
                    "c2_body_points": second["body_points"],
                    "candle_neutral_threshold": max(
                        first["neutral_threshold"], second["neutral_threshold"]
                    ),
                    "momentum_pattern": pattern,
                    "momentum_balance_version": PATCH_VERSION,
                }
            )

            previous_signal = dict(scan.get("signal_data") or {})
            fixed_signal = balanced_get_full_signal(
                market,
                consecutive_losses=loss_streak,
                profile=profile,
            )
            if isinstance(fixed_signal, dict):
                for key, value in previous_signal.items():
                    fixed_signal.setdefault(key, value)
                fixed_signal.setdefault(
                    "strategy_profile_key",
                    (profile or {}).get("profile_key", "okai_default_82"),
                )
                fixed_signal.setdefault(
                    "strategy_profile_name",
                    (profile or {}).get("profile_name", "OKAI Default 82"),
                )

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

    # angel_fetcher imported get_full_signal as a module alias.  Patch both
    # references so direct strategy calls and AUTO scans remain consistent.
    strategy.get_full_signal = balanced_get_full_signal
    angel_fetcher.get_full_signal = balanced_get_full_signal
    runtime._build_scan = build_scan_with_balanced_momentum
    runtime._okai_balanced_momentum_v1 = True


def _cas_hero_status(original_status: dict | None = None) -> dict:
    result = dict(original_status or {})
    current = _now_ist()
    if not _cas_day(current):
        result.setdefault("force_exit_ist", "15:25")
        result.setdefault("closing_system", "LEGACY_LAST_30M_VWAP")
        return result

    minute = _minute_of_day(current)
    active = (14 * 60 + 30) <= minute < 15 * 60
    in_force_exit = 15 * 60 <= minute < CAS_SAFE_EXIT_MINUTE

    if minute < 14 * 60 + 30:
        remaining = 14 * 60 + 30 - minute
        status = f"Opens in {remaining // 60}h {remaining % 60}m"
    elif active:
        status = f"ACTIVE — {15 * 60 - minute}m entry window remaining"
    elif in_force_exit:
        status = f"CAS safety exit in {CAS_SAFE_EXIT_MINUTE - minute}m"
    else:
        status = "Window closed for today"

    result.update(
        {
            "active": active,
            "in_force_exit": in_force_exit,
            "status": status,
            "force_exit_ist": "15:12",
            "cas_guard_active": True,
            "cas_window_ist": "15:15-15:35",
            "derivatives_close_ist": "15:40",
            "closing_system": "SEBI_CLOSING_AUCTION_SESSION",
        }
    )
    return result


def apply_cas_closing_guard_patch() -> None:
    """Install the final date-aware pre-CAS exit and state metadata guard."""

    if getattr(runtime, "_okai_cas_closing_guard_v1", False):
        return

    original_evaluate_exit = runtime._evaluate_exit
    original_state_update = runtime._state_update
    original_hero_status = strategy.is_hero_window_active

    def evaluate_exit_with_cas_guard(trade, ltp, market_data, candle_id):
        result = original_evaluate_exit(trade, ltp, market_data, candle_id)
        if not isinstance(result, dict):
            return result

        current = _now_ist()
        if (
            _cas_day(current)
            and _minute_of_day(current) >= CAS_SAFE_EXIT_MINUTE
            and not result.get("reason")
        ):
            result = dict(result)
            result["reason"] = "CAS SAFETY EXIT 15:12 IST"
            result["cas_guard"] = {
                "version": PATCH_VERSION,
                "cas_start_ist": "15:15",
                "safe_exit_ist": "15:12",
                "derivatives_close_ist": "15:40",
            }
        return result

    def state_update_with_cas_metadata(state, scans, selected, settings, rows):
        original_state_update(state, scans, selected, settings, rows)
        current = _now_ist()
        if _cas_day(current):
            state.update(
                {
                    "closing_system": "SEBI_CLOSING_AUCTION_SESSION",
                    "cas_guard_active": True,
                    "cas_effective_date": CAS_EFFECTIVE_DATE.isoformat(),
                    "cas_window_ist": "15:15-15:35",
                    "derivatives_close_ist": "15:40",
                    "hard_eod_exit_ist": "15:12",
                    "cas_feed_mode": "NO_INDICATIVE_AUCTION_FEED_PRE_CLOSE_EXIT",
                }
            )

    def hero_status_with_cas_guard():
        return _cas_hero_status(original_hero_status())

    runtime._evaluate_exit = evaluate_exit_with_cas_guard
    runtime._state_update = state_update_with_cas_metadata
    strategy.is_hero_window_active = hero_status_with_cas_guard
    angel_fetcher.is_hero_window_active = hero_status_with_cas_guard
    routes.is_hero_window_active = hero_status_with_cas_guard
    runtime._okai_cas_closing_guard_v1 = True
