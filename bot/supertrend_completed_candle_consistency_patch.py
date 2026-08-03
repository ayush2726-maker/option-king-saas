"""Keep AUTO Supertrend direction tied to the last completed candle.

The AUTO runtime intentionally trades from ``df.iloc[-2]`` so the forming
1-minute candle cannot repaint an entry.  This patch makes every related field
use that same completed candle and rebuilds the signal if the displayed
Supertrend direction is missing or inconsistent.

No threshold, position sizing, SL, exit, cooldown or broker-order rule is
changed.  It only repairs indicator data consistency before the existing
mandatory structure and active-profile wrappers run.
"""

from __future__ import annotations

from typing import Any

from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _direction(value: Any) -> str:
    text = str(value or "NEUTRAL").upper().strip()
    return text if text in {"UP", "DOWN"} else "NEUTRAL"


def _completed_snapshot(dataframe: Any) -> dict[str, Any] | None:
    """Return a coherent snapshot from the last fully completed candle."""
    result = angel_fetcher.calculate_indicators(dataframe.copy())
    if result is None:
        return None

    frame, _unused_latest_trend = result
    if frame is None or len(frame) < 2:
        return None

    last = frame.iloc[-2]
    price = _f(last.get("close"), 0.0)
    ema9 = _f(last.get("EMA9"), price)
    ema21 = _f(last.get("EMA21"), price)
    raw_direction = _direction(last.get("ST_DIR"))
    line = _f(last.get("SUPERTREND"), 0.0)

    direction = raw_direction
    repaired = False
    if direction == "NEUTRAL" and price > 0 and line > 0:
        direction = "UP" if price > line else "DOWN" if price < line else "NEUTRAL"
        repaired = direction in {"UP", "DOWN"}

    # Standard Supertrend should always have a numeric active line.  Retain a
    # defensive fallback for older frames so production never silently turns a
    # valid completed candle into NEUTRAL just because one column was omitted.
    if line <= 0:
        if direction == "UP":
            line = _f(last.get("LOWER"), 0.0)
        elif direction == "DOWN":
            line = _f(last.get("UPPER"), 0.0)

    if direction == "NEUTRAL" and price > 0 and line > 0:
        direction = "UP" if price > line else "DOWN" if price < line else "NEUTRAL"
        repaired = direction in {"UP", "DOWN"}

    trend = "UPTREND" if ema9 > ema21 else "DOWNTREND" if ema9 < ema21 else "SIDEWAYS"

    return {
        "frame": frame,
        "row": last,
        "price": price,
        "ema9": ema9,
        "ema21": ema21,
        "trend": trend,
        "supertrend_dir": direction,
        "supertrend_dir_raw": raw_direction,
        "supertrend_value": line if line > 0 else None,
        "supertrend_repaired": repaired,
        "indicator_candle_time": str(last.get("time")),
    }


def _repair_scan(scan: Any, dataframe: Any, profile: dict, loss_streak: int) -> Any:
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan

    try:
        snapshot = _completed_snapshot(dataframe)
        if not snapshot:
            return scan

        market = dict(scan.get("market_data") or {})
        market.update(
            {
                "price": snapshot["price"],
                "ema9": snapshot["ema9"],
                "ema21": snapshot["ema21"],
                "trend": snapshot["trend"],
                "mtf_confirmed": snapshot["trend"] != "SIDEWAYS",
                "supertrend_dir": snapshot["supertrend_dir"],
                "supertrend_value": snapshot["supertrend_value"],
                "supertrend_dir_raw": snapshot["supertrend_dir_raw"],
                "supertrend_completed_candle": True,
                "supertrend_repaired": snapshot["supertrend_repaired"],
                "indicator_candle_time": snapshot["indicator_candle_time"],
                "forming_candle_ignored": True,
            }
        )

        # Rebuild the decision from the corrected completed-candle snapshot.
        # Later wrappers still attach safety guards and the active-profile score
        # breakdown, preserving the established production patch order.
        signal = angel_fetcher.get_full_signal(
            market,
            consecutive_losses=loss_streak,
            profile=profile,
        )
        if isinstance(signal, dict):
            signal.setdefault(
                "strategy_profile_key",
                profile.get("profile_key", "okai_default_82"),
            )
            signal.setdefault(
                "strategy_profile_name",
                profile.get("profile_name", "OKAI Default 82"),
            )
            signal["indicator_candle_time"] = snapshot["indicator_candle_time"]
            signal["supertrend_value"] = snapshot["supertrend_value"]
            signal["supertrend_dir_raw"] = snapshot["supertrend_dir_raw"]
            signal["supertrend_repaired"] = snapshot["supertrend_repaired"]
            scan["signal_data"] = signal
            market["signal"] = signal.get("signal", "WAIT")
            market["signal_score"] = signal.get("score", 0)
            market["signal_min_score"] = signal.get("min_score", 82)

        scan["market_data"] = market
        scan["chart_candles"] = angel_fetcher.build_chart_candles(
            snapshot["frame"],
            limit=390,
        )
        scan["candle_id"] = snapshot["indicator_candle_time"]
        return scan
    except Exception as exc:
        try:
            scan["supertrend_consistency_error"] = str(exc)[:160]
        except Exception:
            pass
        return scan


def apply_supertrend_completed_candle_consistency_patch() -> None:
    if getattr(runtime, "_okai_supertrend_completed_candle_v1", False):
        return

    original_build_scan = runtime._build_scan

    def build_scan_with_completed_supertrend(
        user_id,
        underlying,
        df,
        profile,
        loss_streak,
    ):
        scan = original_build_scan(user_id, underlying, df, profile, loss_streak)
        return _repair_scan(scan, df, profile or {}, loss_streak)

    runtime._build_scan = build_scan_with_completed_supertrend
    runtime._okai_supertrend_completed_candle_v1 = True
