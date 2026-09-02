"""Final normal-AUTO regime and fresh-breakout protection.

The score pipeline has accumulated several permissive replay wrappers.  This
module runs at both real production scan boundaries and fails closed only for
the concrete whipsaw patterns that caused the 02-Sep-2026 loss cluster:

* a >=0.60% gap during the first 30 minutes;
* disagreement between VWAP, EMA, Supertrend and real completed 5-minute MTF;
* no fresh two-candle break of the prior six completed one-minute candles.

It does not change capital sizing, daily P&L limits, ATR stops, Hero Zero or
manual entries.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from bot import auto_portfolio_runtime as runtime


VERSION = "OKAI-MARKET-REGIME-SHIELD-V2"
GAP_THRESHOLD_PERCENT = 0.60
GAP_WAIT_UNTIL_MINUTE = 9 * 60 + 45
SWING_LOOKBACK = 6
SWING_BREAK_BUFFER_ATR = 0.05
GAP_BLOCK_REASON = "GAP_OPEN_WAIT_UNTIL_0945_IST"
ALIGNMENT_BLOCK_REASON = "FINAL_VWAP_ST_EMA_MTF_MISALIGNED"
FRESH_BREAK_BLOCK_REASON = "FRESH_2_CANDLE_SWING_BREAK_REQUIRED"

_PREVIOUS_CLOSE_CACHE: dict[tuple[int, str, str], float | None] = {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _side(signal: dict[str, Any]) -> str:
    value = str(
        signal.get("candidate_signal")
        or signal.get("signal")
        or "WAIT"
    ).upper()
    return value if value in {"CE", "PE"} else "WAIT"


def _candle_time(scan: dict[str, Any], frame: Any) -> datetime:
    raw = scan.get("candle_id")
    if raw is None:
        try:
            raw = frame.iloc[-2].get("time")
        except Exception:
            raw = None
    try:
        import pandas as pd

        value = pd.to_datetime(raw, errors="coerce", utc=True)
        if pd.notna(value):
            return value.to_pydatetime().astimezone(
                timezone(timedelta(hours=5, minutes=30))
            )
    except Exception:
        pass
    return runtime._now_ist()


def _frame_previous_close(frame: Any, trading_day: date) -> float | None:
    try:
        import pandas as pd

        work = frame.copy()
        work["time"] = pd.to_datetime(work["time"], errors="coerce", utc=True)
        local_day = work["time"].dt.tz_convert("Asia/Kolkata").dt.date
        previous = work[local_day < trading_day]
        if not previous.empty:
            value = _f(previous.iloc[-1].get("close"), 0.0)
            return value if value > 0 else None
    except Exception:
        pass
    return None


def _saved_previous_close(
    user_id: int,
    underlying: str,
    trading_day: date,
) -> float | None:
    key = (int(user_id), str(underlying).upper(), trading_day.isoformat())
    if key in _PREVIOUS_CLOSE_CACHE:
        return _PREVIOUS_CLOSE_CACHE[key]

    result = None
    try:
        from database import get_db

        conn = get_db()
        try:
            row = conn.execute(
                """
                SELECT price
                FROM signal_history
                WHERE user_id=? AND UPPER(instrument)=?
                  AND CAST(price AS REAL)>0
                  AND date(datetime(created_at, '+330 minutes')) < ?
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT 1
                """,
                (int(user_id), str(underlying).upper(), trading_day.isoformat()),
            ).fetchone()
            value = _f(row["price"] if row else 0.0, 0.0)
            result = value if value > 0 else None
        finally:
            conn.close()
    except Exception:
        result = None

    _PREVIOUS_CLOSE_CACHE[key] = result
    return result


def _opening_price(frame: Any, trading_day: date) -> float | None:
    try:
        import pandas as pd

        work = frame.copy()
        if "time" in work.columns:
            work["time"] = pd.to_datetime(work["time"], errors="coerce", utc=True)
            local_day = work["time"].dt.tz_convert("Asia/Kolkata").dt.date
            today = work[local_day == trading_day]
            if not today.empty:
                work = today
        value = _f(work.iloc[0].get("open"), 0.0)
        return value if value > 0 else None
    except Exception:
        return None


def _fresh_breakout(frame: Any, candidate: str, atr: float) -> dict[str, Any]:
    result = {
        "passed": False,
        "two_candle_momentum": False,
        "swing_breakout": False,
        "lookback": SWING_LOOKBACK,
    }
    try:
        # The last row may still be forming.  Match the rest of AUTO by using
        # -2 as the latest completed candle.
        latest = frame.iloc[-2]
        previous = frame.iloc[-3]
        prior = frame.iloc[-(SWING_LOOKBACK + 2):-2]
        if len(prior) < SWING_LOOKBACK:
            return result

        latest_open = _f(latest.get("open"))
        latest_close = _f(latest.get("close"))
        previous_open = _f(previous.get("open"))
        previous_close = _f(previous.get("close"))
        buffer_points = max(0.01, _f(atr, 0.0) * SWING_BREAK_BUFFER_ATR)

        if candidate == "CE":
            momentum = previous_close > previous_open and latest_close > latest_open
            level = max(_f(value) for value in prior["high"])
            breakout = latest_close >= level + buffer_points
        elif candidate == "PE":
            momentum = previous_close < previous_open and latest_close < latest_open
            level = min(_f(value) for value in prior["low"])
            breakout = latest_close <= level - buffer_points
        else:
            momentum = False
            breakout = False
            level = 0.0

        result.update(
            {
                "passed": bool(momentum and breakout),
                "two_candle_momentum": bool(momentum),
                "swing_breakout": bool(breakout),
                "swing_level": round(level, 4),
                "buffer_points": round(buffer_points, 4),
                "latest_close": round(latest_close, 4),
            }
        )
    except Exception:
        pass
    return result


def _alignment(market: dict[str, Any], signal: dict[str, Any], candidate: str) -> dict[str, Any]:
    price = _f(market.get("price"), 0.0)
    vwap = _f(market.get("vwap"), price)
    ema9 = _f(market.get("ema9"), price)
    ema21 = _f(market.get("ema21"), ema9)
    supertrend = str(market.get("supertrend_dir") or "NEUTRAL").upper()
    mtf = dict(signal.get("real_mtf_5m") or {})
    mtf_side = str(mtf.get("side") or "WAIT").upper()
    mtf_ok = bool(mtf.get("available", False) and mtf_side == candidate)

    if candidate == "CE":
        vwap_ok = price > vwap
        ema_ok = ema9 > ema21
        supertrend_ok = supertrend == "UP"
    elif candidate == "PE":
        vwap_ok = price < vwap
        ema_ok = ema9 < ema21
        supertrend_ok = supertrend == "DOWN"
    else:
        vwap_ok = ema_ok = supertrend_ok = False

    return {
        "passed": bool(vwap_ok and ema_ok and supertrend_ok and mtf_ok),
        "vwap": bool(vwap_ok),
        "ema": bool(ema_ok),
        "supertrend": bool(supertrend_ok),
        "real_5m_mtf": bool(mtf_ok),
        "real_5m_side": mtf_side,
    }


def _block(scan: dict[str, Any], reason: str) -> None:
    signal = scan["signal_data"]
    market = scan["market_data"]
    signal["signal"] = "WAIT"
    signal["trade_allowed"] = False
    signal["strategy_qualified"] = False
    signal["safety_gate_passed"] = False
    signal["execution_allowed"] = False
    signal["execution_block_reason"] = reason
    reasons = list(signal.get("safety_gate_reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    signal["safety_gate_reasons"] = reasons
    market["signal"] = "WAIT"
    market["execution_allowed"] = False
    market["execution_block_reason"] = reason
    scan["execution_allowed"] = False
    scan["execution_block_reason"] = reason


def _apply_shield(
    scan: Any,
    frame: Any,
    user_id: int,
    underlying: str,
    previous_close: float | None = None,
    now_ist: datetime | None = None,
) -> Any:
    if not isinstance(scan, dict) or scan.get("status") != "OK":
        return scan
    signal = scan.get("signal_data")
    market = scan.get("market_data")
    if not isinstance(signal, dict) or not isinstance(market, dict):
        return scan

    candidate = _side(signal)
    current = now_ist or _candle_time(scan, frame)
    trading_day = current.date()
    prior_close = previous_close or _frame_previous_close(frame, trading_day)
    if not prior_close:
        prior_close = _saved_previous_close(user_id, underlying, trading_day)
    opening = _opening_price(frame, trading_day)
    gap_percent = (
        (opening / prior_close - 1.0) * 100.0
        if opening and prior_close
        else None
    )
    gap_day = bool(
        gap_percent is not None
        and abs(gap_percent) + 1e-9 >= GAP_THRESHOLD_PERCENT
    )
    market["previous_close"] = round(prior_close, 4) if prior_close else None
    market["opening_price"] = round(opening, 4) if opening else None
    market["gap_percent"] = round(gap_percent, 3) if gap_percent is not None else None
    market["gap_day"] = gap_day

    alignment = _alignment(market, signal, candidate)
    fresh = _fresh_breakout(frame, candidate, _f(market.get("atr"), 0.0))
    minute = current.hour * 60 + current.minute
    gap_wait = bool(gap_day and minute < GAP_WAIT_UNTIL_MINUTE)
    diagnostics = {
        "version": VERSION,
        "candidate": candidate,
        "gap_day": gap_day,
        "gap_percent": market["gap_percent"],
        "gap_wait_active": gap_wait,
        "alignment": alignment,
        "fresh_breakout": fresh,
        "checked_at_ist": current.isoformat(),
    }
    signal["market_regime_shield"] = diagnostics
    market["market_regime_shield"] = diagnostics
    scan["market_regime_shield"] = diagnostics

    # Never revive a setup already rejected by a preceding safety layer.
    qualified = bool(
        candidate in {"CE", "PE"}
        and signal.get("trade_allowed", False)
        and str(signal.get("signal") or "WAIT").upper() in {"CE", "PE"}
    )
    if not qualified:
        return scan
    if gap_wait:
        _block(scan, GAP_BLOCK_REASON)
    elif not alignment["passed"]:
        _block(scan, ALIGNMENT_BLOCK_REASON)
    elif not fresh["passed"]:
        _block(scan, FRESH_BREAK_BLOCK_REASON)
    return scan


def apply_market_regime_shield_v2() -> bool:
    if getattr(runtime, "_okai_market_regime_shield_v2", False):
        return True

    previous_build_scan = runtime._build_scan

    def build_scan_with_regime_shield(user_id, underlying, frame, profile, loss_streak):
        return _apply_shield(
            previous_build_scan(user_id, underlying, frame, profile, loss_streak),
            frame,
            user_id,
            underlying,
        )

    runtime._build_scan = build_scan_with_regime_shield

    try:
        from bot import live_scan_history_fallback_patch as replay

        previous_replay_scan = replay._replay_scan

        def replay_scan_with_regime_shield(
            user_id,
            underlying,
            frame,
            profile,
            source,
            notes,
        ):
            return _apply_shield(
                previous_replay_scan(
                    user_id,
                    underlying,
                    frame,
                    profile,
                    source,
                    notes,
                ),
                frame,
                user_id,
                underlying,
            )

        replay._replay_scan = replay_scan_with_regime_shield
        replay._okai_market_regime_shield_v2 = True
    except Exception:
        pass

    runtime._okai_market_regime_shield_v2 = True
    runtime._okai_market_regime_shield_version = VERSION
    return True


__all__ = [
    "ALIGNMENT_BLOCK_REASON",
    "FRESH_BREAK_BLOCK_REASON",
    "GAP_BLOCK_REASON",
    "VERSION",
    "_apply_shield",
    "_fresh_breakout",
    "apply_market_regime_shield_v2",
]
