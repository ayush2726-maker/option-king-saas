"""Keep live/Paper direction calculations on one completed candle.

AUTO Portfolio previously used price, EMA and Supertrend from ``df.iloc[-2]``
but reused the trend label calculated from ``df.iloc[-1]``.  The last row is the
currently forming candle, so a fast reversal could mix bullish and bearish
states in one signal.  This was especially visible as a one-sided CE/PE bias.

This patch recomputes the EMA trend from the same completed candle used for all
other directional fields, then reruns the final signal pipeline.  It does not
force an opposite-side trade and it does not change score 82; it only removes
the mixed-candle direction bug.
"""

from __future__ import annotations

from bot import auto_portfolio_runtime as runtime


def _completed_trend(market: dict) -> str:
    try:
        ema9 = float(market.get("ema9") or 0)
        ema21 = float(market.get("ema21") or 0)
    except Exception:
        return "SIDEWAYS"

    if ema9 > ema21:
        return "UPTREND"
    if ema9 < ema21:
        return "DOWNTREND"
    return "SIDEWAYS"


def apply_completed_candle_direction_patch() -> None:
    if getattr(runtime, "_okai_completed_candle_direction_v1", False):
        return

    original_build_scan = runtime._build_scan

    def build_scan_same_candle(user_id, underlying, df, profile, loss_streak):
        scan = original_build_scan(
            user_id,
            underlying,
            df,
            profile,
            loss_streak,
        )
        if not isinstance(scan, dict) or scan.get("status") != "OK":
            return scan

        market = dict(scan.get("market_data") or {})
        old_trend = str(market.get("trend") or "SIDEWAYS").upper()
        trend = _completed_trend(market)
        market["trend"] = trend
        market["mtf_confirmed"] = trend != "SIDEWAYS"
        market["direction_candle_source"] = "LAST_COMPLETED_CANDLE"
        market["previous_mixed_candle_trend"] = old_trend

        # Resolve through angel_fetcher dynamically. Later startup patches
        # (mandatory VWAP/ST/EMA and entry timing) therefore remain active.
        legacy = runtime._legacy()
        signal = legacy.get_full_signal(
            market,
            consecutive_losses=loss_streak,
            profile=profile,
        )
        if not isinstance(signal, dict):
            return scan

        signal = dict(signal)
        signal.setdefault(
            "strategy_profile_key",
            profile.get("profile_key", "okai_default_82"),
        )
        signal.setdefault(
            "strategy_profile_name",
            profile.get("profile_name", "OKAI Default 82"),
        )
        signal["direction_candle_source"] = "LAST_COMPLETED_CANDLE"
        signal["completed_candle_trend"] = trend
        signal["mixed_candle_direction_fixed"] = old_trend != trend

        market["signal"] = signal.get("signal", "WAIT")
        market["signal_score"] = signal.get("score", 0)
        market["signal_min_score"] = signal.get("min_score", 82)

        scan["market_data"] = market
        scan["signal_data"] = signal
        return scan

    runtime._build_scan = build_scan_same_candle
    runtime._okai_completed_candle_direction_v1 = True
