"""Final Supertrend repair for the authoritative replay-first AUTO scan.

The older neutral-line fallback wrapped ``runtime._build_scan``. Production AUTO
now uses ``live_scan_history_fallback_patch._replay_scan`` instead, so a valid
completed-candle Supertrend could be calculated but the replay payload still
exposed ``NEUTRAL`` and the mandatory gate blocked the trade.

This patch derives direction only from the completed indicator candle produced by
the existing standard Supertrend implementation. It never guesses without a valid
numeric line/price and does not change threshold, score weights, orders or risk.
"""

from __future__ import annotations

import math

from bot import angel_fetcher
from bot import live_scan_history_fallback_patch as replay
from bot import mandatory_trend_structure_patch as mandatory

VERSION = "SUPERTREND_REPLAY_FINAL_V2"
_STALE_REASON = "SUPERTREND_DIRECTION_REQUIRED"


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def _direction_from_completed_frame(frame):
    """Return (direction, line, close, candle_time) from the completed candle."""
    if frame is None or getattr(frame, "empty", True):
        return "NEUTRAL", None, None, None

    try:
        result = angel_fetcher.calculate_indicators(frame.copy())
        if not result:
            return "NEUTRAL", None, None, None
        indicators, _trend = result
        if indicators is None or getattr(indicators, "empty", True):
            return "NEUTRAL", None, None, None

        # Same completed-candle convention as replay scan: latest row may form.
        row = indicators.iloc[-2] if len(indicators) >= 2 else indicators.iloc[-1]
        close = _number(row.get("close"))
        line = _number(
            row.get("SUPERTREND")
            if row.get("SUPERTREND") is not None
            else row.get("supertrend")
        )
        direction = str(row.get("ST_DIR") or "NEUTRAL").upper()

        if direction not in {"UP", "DOWN"} and close and line and close != line:
            direction = "UP" if close > line else "DOWN"

        if direction not in {"UP", "DOWN"}:
            return "NEUTRAL", line, close, row.get("time")
        return direction, line, close, row.get("time")
    except Exception:
        return "NEUTRAL", None, None, None


def _without_stale_supertrend_reason(values):
    output = []
    for value in values or []:
        text = str(value or "").strip()
        if text == _STALE_REASON:
            continue
        if text and text not in output:
            output.append(text)
    return output


def _repair_replay_scan(scan, frame):
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan

    market = scan.get("market_data")
    signal = scan.get("signal_data")
    if not isinstance(market, dict) or not isinstance(signal, dict):
        return scan

    current = str(market.get("supertrend_dir") or "NEUTRAL").upper()
    if current in {"UP", "DOWN"}:
        market.setdefault("supertrend_source", "REPLAY_PAYLOAD")
        return scan

    direction, line, close, candle_time = _direction_from_completed_frame(frame)
    if direction not in {"UP", "DOWN"}:
        market["supertrend_source"] = "COMPLETED_CANDLE_UNAVAILABLE"
        market["supertrend_repair_version"] = VERSION
        return scan

    market["supertrend_dir"] = direction
    market["supertrend_value"] = line
    market["supertrend_completed_close"] = close
    market["supertrend_completed_candle"] = str(candle_time or scan.get("candle_id") or "")
    market["supertrend_source"] = "COMPLETED_STANDARD_SUPERTREND"
    market["supertrend_repair_version"] = VERSION

    cleaned = dict(signal)
    cleaned["safety_gate_reasons"] = _without_stale_supertrend_reason(
        cleaned.get("safety_gate_reasons")
    )
    cleaned["fresh_entry_block_reasons"] = _without_stale_supertrend_reason(
        cleaned.get("fresh_entry_block_reasons")
    )
    cleaned["warnings"] = _without_stale_supertrend_reason(cleaned.get("warnings"))

    repaired_signal = mandatory._normalize(cleaned, market)
    scan["signal_data"] = repaired_signal
    market["signal"] = repaired_signal.get("signal", "WAIT")
    market["signal_score"] = repaired_signal.get("score", 0)

    # Keep the chart payload and details consistent with the actual gate.
    chart = scan.get("chart_candles") or []
    target_time = str(scan.get("candle_id") or "")
    for candle in reversed(chart):
        if not isinstance(candle, dict):
            continue
        if target_time and str(candle.get("time") or "") != target_time:
            continue
        candle["supertrend_dir"] = direction
        candle["supertrend"] = line
        candle["supertrend_source"] = "COMPLETED_STANDARD_SUPERTREND"
        break

    note = str(scan.get("data_note") or "")
    marker = f"st={direction} source=completed_standard"
    scan["data_note"] = (note + " | " + marker).strip(" |")[:500]
    return scan


def apply_supertrend_replay_final_patch() -> None:
    if getattr(replay, "_okai_supertrend_replay_final_v2", False):
        return

    original_replay_scan = replay._replay_scan

    def replay_scan_with_final_supertrend(
        user_id,
        underlying,
        frame,
        profile,
        source,
        notes,
    ):
        scan = original_replay_scan(
            user_id,
            underlying,
            frame,
            profile,
            source,
            notes,
        )
        return _repair_replay_scan(scan, frame)

    replay._replay_scan = replay_scan_with_final_supertrend
    replay._okai_supertrend_replay_final_v2 = True
    replay._okai_supertrend_replay_final_version = VERSION
