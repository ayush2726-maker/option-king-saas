"""Real premium direction confirmation for AUTO entries.

This is not a post-loss block. It fixes the root failure mode where the index
score can be high while the selected option premium is already falling. For a
bought CE/PE entry, the option premium itself must show live continuation before
AUTO Portfolio opens the trade.

Risk/order mechanics, SL math, position sizing and exit rules are unchanged.
"""
from __future__ import annotations

from typing import Any


VERSION = "OKAI-ENTRY-DIRECTION-CONFIRMATION-V1"
BLOCK_REASON = "OPTION_PREMIUM_DIRECTION_NOT_CONFIRMED"
FETCH_REASON = "OPTION_DIRECTION_CANDLES_REQUIRED"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _pct(now: float, old: float) -> float:
    old = max(0.05, _f(old, 0.05))
    return (now / old - 1.0) * 100.0


def _decorate_block(base: dict, reason: str, **extra) -> dict:
    result = dict(base or {})
    result.update({
        "allowed": False,
        "reason": reason,
        "version": VERSION,
        "root_fix": "ENTRY_REQUIRES_REAL_OPTION_PREMIUM_CONTINUATION",
        **extra,
    })
    return result


def apply_entry_direction_confirmation_patch() -> None:
    from bot import angel_fetcher

    if getattr(angel_fetcher, "_okai_entry_direction_confirmation_v1", False):
        return

    original_quality = angel_fetcher._premium_entry_quality

    def premium_entry_quality_with_direction(rows, current_ltp):
        base = dict(original_quality(rows, current_ltp) or {})
        if base.get("allowed") is False:
            base.setdefault("version", VERSION)
            return base

        candles = angel_fetcher._normalize_option_candles(rows)
        current = _f(current_ltp, 0.0)
        if current <= 0:
            return _decorate_block(base, "INVALID_OPTION_LTP", current_ltp=current)

        # Earlier logic allowed entries when option candles were unavailable. That
        # caused high index-score trades to enter even when premium direction was
        # unknown. For bought options this must be a no-trade until candles exist.
        if len(candles) < 5:
            return _decorate_block(
                base,
                FETCH_REASON,
                current_ltp=round(current, 2),
                candle_count=len(candles),
            )

        recent = candles[-5:]
        latest = recent[-1]
        previous = recent[-2]
        closes = [float(c["close"]) for c in recent]
        highs = [float(c["high"]) for c in recent]
        lows = [float(c["low"]) for c in recent]

        latest_body_pct = _pct(float(latest["close"]), float(latest["open"]))
        current_vs_latest_close_pct = _pct(current, float(latest["close"]))
        current_vs_previous_close_pct = _pct(current, float(previous["close"]))
        current_vs_recent_low_pct = _pct(current, min(lows[-3:]))
        current_vs_recent_high_pct = _pct(current, max(highs[-3:]))

        lower_close_streak = closes[-1] < closes[-2] < closes[-3]
        fresh_breakdown = current < min(float(latest["low"]), float(previous["close"]))
        bearish_latest = latest_body_pct <= -0.60
        current_fading = current_vs_previous_close_pct <= -0.35 and current_vs_latest_close_pct <= -0.20

        # Positive proof for bought option: either the current premium is extending
        # above latest/previous close, or latest completed option candle was green
        # and current is holding that close. This prevents buying a CE/PE whose
        # own premium has already started falling.
        continuation_ok = (
            current_vs_previous_close_pct >= 0.25
            or current_vs_latest_close_pct >= 0.15
            or (latest_body_pct >= 0.35 and current_vs_latest_close_pct >= -0.15)
        )

        direction_failed = (
            not continuation_ok
            or lower_close_streak
            or fresh_breakdown
            or bearish_latest
            or current_fading
        )

        result = dict(base)
        result.update({
            "version": VERSION,
            "premium_direction_confirmed": not direction_failed,
            "current_ltp": round(current, 2),
            "candle_count": len(candles),
            "latest_body_pct": round(latest_body_pct, 2),
            "current_vs_latest_close_pct": round(current_vs_latest_close_pct, 2),
            "current_vs_previous_close_pct": round(current_vs_previous_close_pct, 2),
            "current_vs_recent_low_pct": round(current_vs_recent_low_pct, 2),
            "current_vs_recent_high_pct": round(current_vs_recent_high_pct, 2),
            "lower_close_streak": bool(lower_close_streak),
            "fresh_breakdown": bool(fresh_breakdown),
            "bearish_latest": bool(bearish_latest),
            "current_fading": bool(current_fading),
            "continuation_ok": bool(continuation_ok),
        })

        if direction_failed:
            result["allowed"] = False
            result["reason"] = BLOCK_REASON
        else:
            result["allowed"] = True
            result["reason"] = "OPTION_PREMIUM_DIRECTION_CONFIRMED"

        return result

    angel_fetcher._premium_entry_quality = premium_entry_quality_with_direction
    angel_fetcher._okai_entry_direction_confirmation_v1 = True
