"""Final AUTO protection for real 5-minute confirmation and session direction.

The live replay path previously labelled the current 1-minute EMA direction as
"MTF confirmed" and awarded the 10-point 5-minute bonus.  A short pullback could
therefore qualify a PE trade during a strong bullish session (or CE during a
strong bearish session).  PAPER AUTO was also allowed to open fresh positions
until 15:25, despite the normal strategy cutoff being 14:45.

This patch is installed after the existing score/display wrappers and:
- derives MTF from completed, exchange-aligned 5-minute candles;
- removes the old same-timeframe MTF bonus and awards it only when real 5-minute
  EMA and Supertrend agree with the 1-minute candidate;
- blocks an opposite-side entry when both the real 5-minute trend and the
  completed ORB show a clear session bias;
- restores 14:45 as the normal AUTO entry cutoff for PAPER and LIVE, while
  preserving the separate Hero Zero route and the 15:25 EOD exit.

No position sizing, ATR SL, profit lock, cooldown, broker order, or Hero Zero
rule is changed.
"""

from __future__ import annotations

from typing import Any

from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime


PATCH_VERSION = "REAL_5M_MTF_SESSION_GUARD_V1"
NORMAL_AUTO_CUTOFF_MINUTE = 14 * 60 + 45
MIN_COMPLETE_5M_BARS = 12
ORB_BUFFER_POINTS = 5.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _direction(value: Any) -> str:
    text = str(value or "NEUTRAL").upper().strip()
    return text if text in {"UP", "DOWN"} else "NEUTRAL"


def _candidate_side(signal: dict) -> str:
    value = str(
        signal.get("candidate_signal")
        or signal.get("signal")
        or "WAIT"
    ).upper()
    return value if value in {"CE", "PE"} else "WAIT"


def _mtf_weight(profile: dict, signal: dict) -> int:
    weights = signal.get("profile_weights")
    if not isinstance(weights, dict):
        weights = (profile or {}).get("weights")
    if isinstance(weights, dict):
        return max(0, _i(weights.get("mtf"), 10))
    return 10


def _mtf_enabled(profile: dict, signal: dict) -> bool:
    enabled = signal.get("profile_enabled")
    if not isinstance(enabled, dict):
        enabled = (profile or {}).get("enabled")
    return not isinstance(enabled, dict) or enabled.get("mtf", True) is not False


def _old_mtf_bonus(signal: dict, market: dict, weight: int) -> int:
    for value in (
        signal.get("mtf_bonus"),
        (signal.get("score_breakdown") or {}).get("mtf"),
    ):
        if value is not None:
            return max(0, _i(value, 0))

    payload = signal.get("live_score_breakdown") or {}
    for component in payload.get("components") or []:
        key = str(component.get("key") or component.get("label") or "").lower()
        if "mtf" in key or "multi" in key:
            return max(
                0,
                _i(component.get("decision_score", component.get("score", 0)), 0),
            )

    return weight if bool(signal.get("mtf_confirmed", market.get("mtf_confirmed"))) else 0


def _completed_5m_snapshot(frame: Any, candle_id: Any) -> dict[str, Any]:
    import pandas as pd

    if frame is None or getattr(frame, "empty", True):
        return {"available": False, "reason": "REAL_5M_FRAME_EMPTY"}

    work = frame.copy()
    required = {"time", "open", "high", "low", "close"}
    if not required.issubset(set(work.columns)):
        return {"available": False, "reason": "REAL_5M_COLUMNS_MISSING"}

    work["time"] = pd.to_datetime(work["time"], errors="coerce", utc=True)
    for column in ("open", "high", "low", "close", "volume"):
        if column not in work.columns:
            work[column] = 0.0
        work[column] = pd.to_numeric(work[column], errors="coerce")

    work = (
        work.dropna(subset=["time", "open", "high", "low", "close"])
        .drop_duplicates(subset=["time"], keep="last")
        .sort_values("time")
    )
    if work.empty:
        return {"available": False, "reason": "REAL_5M_NO_VALID_ROWS"}

    cutoff = pd.to_datetime(candle_id, errors="coerce", utc=True)
    if pd.notna(cutoff):
        work = work[work["time"] <= cutoff]
    if work.empty:
        return {"available": False, "reason": "REAL_5M_NO_COMPLETED_ROWS"}

    local_time = work["time"].dt.tz_convert("Asia/Kolkata")
    session_date = (
        cutoff.tz_convert("Asia/Kolkata").date()
        if pd.notna(cutoff)
        else local_time.iloc[-1].date()
    )
    minute = local_time.dt.hour * 60 + local_time.dt.minute
    work = work[
        (local_time.dt.date == session_date)
        & minute.ge(9 * 60 + 15)
        & minute.lt(15 * 60 + 30)
    ].copy()
    if work.empty:
        return {"available": False, "reason": "REAL_5M_SESSION_EMPTY"}

    work["local_time"] = work["time"].dt.tz_convert("Asia/Kolkata")
    work = work.set_index("local_time")
    grouped = work.resample(
        "5min",
        origin="start_day",
        offset="15min",
        closed="left",
        label="right",
    )
    bars = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        row_count=("close", "count"),
    )
    bars = bars[bars["row_count"] >= 5].drop(columns=["row_count"])
    bars = bars.dropna(subset=["open", "high", "low", "close"]).reset_index()
    bars = bars.rename(columns={"local_time": "time"})

    if len(bars) < MIN_COMPLETE_5M_BARS:
        return {
            "available": False,
            "reason": "REAL_5M_WARMUP",
            "bar_count": int(len(bars)),
        }

    result = angel_fetcher.calculate_indicators(bars.copy())
    if result is None:
        return {
            "available": False,
            "reason": "REAL_5M_INDICATOR_WAIT",
            "bar_count": int(len(bars)),
        }

    calculated, _unused_trend = result
    if calculated is None or calculated.empty:
        return {"available": False, "reason": "REAL_5M_INDICATOR_EMPTY"}

    last = calculated.iloc[-1]
    ema9 = _f(last.get("EMA9"), last.get("close"))
    ema21 = _f(last.get("EMA21"), last.get("close"))
    st_dir = _direction(last.get("ST_DIR"))
    trend = "UPTREND" if ema9 > ema21 else "DOWNTREND" if ema9 < ema21 else "SIDEWAYS"

    if trend == "UPTREND" and st_dir == "UP":
        side = "CE"
    elif trend == "DOWNTREND" and st_dir == "DOWN":
        side = "PE"
    else:
        side = "WAIT"

    candle_time = last.get("time")
    try:
        candle_time = candle_time.isoformat()
    except Exception:
        candle_time = str(candle_time)

    return {
        "available": True,
        "reason": "REAL_5M_OK",
        "bar_count": int(len(calculated)),
        "side": side,
        "trend": trend,
        "supertrend_dir": st_dir,
        "ema9": round(ema9, 4),
        "ema21": round(ema21, 4),
        "close": round(_f(last.get("close"), 0.0), 4),
        "candle_time": candle_time,
    }


def _session_bias(market: dict, mtf: dict) -> str:
    if not mtf.get("available"):
        return "NEUTRAL"

    price = _f(market.get("price"), 0.0)
    orb_high = _f(market.get("orb_high"), 0.0)
    orb_low = _f(market.get("orb_low"), 0.0)
    side = str(mtf.get("side") or "WAIT").upper()

    if side == "CE" and orb_high > 0 and price > orb_high + ORB_BUFFER_POINTS:
        return "CE"
    if side == "PE" and orb_low > 0 and price < orb_low - ORB_BUFFER_POINTS:
        return "PE"
    return "NEUTRAL"


def _clean_dynamic_reasons(values: Any) -> list[str]:
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        upper = text.upper()
        if upper.startswith("SCORE_BELOW_"):
            continue
        if upper.startswith("REAL_5M_MTF_"):
            continue
        if upper.startswith("SESSION_COUNTER_TREND_"):
            continue
        if text not in result:
            result.append(text)
    return result


def _update_breakdown(signal: dict, old_bonus: int, new_bonus: int, confirmed: bool, mtf: dict) -> None:
    breakdown = dict(signal.get("score_breakdown") or {})
    breakdown["mtf"] = new_bonus
    signal["score_breakdown"] = breakdown

    payload = signal.get("live_score_breakdown")
    if not isinstance(payload, dict):
        return

    fixed = dict(payload)
    components = []
    changed = False
    for raw in fixed.get("components") or []:
        item = dict(raw)
        key = str(item.get("key") or item.get("label") or "").lower()
        if "mtf" in key or "multi" in key:
            item["score"] = new_bonus
            item["decision_score"] = new_bonus
            item["passed"] = bool(confirmed)
            item["detail"] = (
                f"Real completed 5m: {mtf.get('trend', 'WAIT')} / "
                f"ST {mtf.get('supertrend_dir', 'NEUTRAL')}"
                if mtf.get("available")
                else f"Real completed 5m unavailable: {mtf.get('reason', 'WAIT')}"
            )
            changed = True
        components.append(item)

    if changed:
        fixed["components"] = components
        visual = sum(max(0, _i(item.get("score"), 0)) for item in components)
        fixed["component_total"] = visual
        fixed["display_score"] = visual
        fixed["visual_strength_score"] = visual

    decision = _i(signal.get("score"), 0)
    fixed["score"] = decision
    fixed["decision_score"] = decision
    fixed["real_mtf_5m"] = dict(mtf)
    signal["live_score_breakdown"] = fixed


def _repair_scan(scan: Any, frame: Any, profile: dict | None = None) -> Any:
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan

    signal = dict(scan.get("signal_data") or {})
    market = dict(scan.get("market_data") or {})
    if not signal or not market:
        return scan

    mtf = _completed_5m_snapshot(frame, scan.get("candle_id"))
    candidate = _candidate_side(signal)
    weight = _mtf_weight(profile or {}, signal)
    enabled = _mtf_enabled(profile or {}, signal)
    old_bonus = _old_mtf_bonus(signal, market, weight)
    confirmed = bool(
        enabled
        and mtf.get("available")
        and candidate in {"CE", "PE"}
        and str(mtf.get("side") or "WAIT").upper() == candidate
    )
    new_bonus = weight if confirmed else 0

    old_score = _i(signal.get("score"), 0)
    new_score = max(0, min(100, old_score - old_bonus + new_bonus))

    market.update(
        {
            "mtf_confirmed": confirmed,
            "mtf_timeframe": "5m",
            "mtf_source": "COMPLETED_5M_CANDLES",
            "mtf_side": mtf.get("side", "WAIT"),
            "mtf_trend": mtf.get("trend", "SIDEWAYS"),
            "mtf_supertrend_dir": mtf.get("supertrend_dir", "NEUTRAL"),
            "mtf_candle_time": mtf.get("candle_time"),
            "mtf_bar_count": mtf.get("bar_count", 0),
            "mtf_available": bool(mtf.get("available")),
            "mtf_reason": mtf.get("reason"),
            "fake_same_timeframe_mtf_removed": True,
        }
    )

    warnings = [
        warning
        for warning in signal.get("warnings") or []
        if "MTF" not in str(warning).upper()
    ]
    if confirmed:
        warnings.append("REAL_5M_MTF_CONFIRMED")
    elif mtf.get("available"):
        warnings.append(
            f"REAL_5M_MTF_NOT_CONFIRMED:{mtf.get('side', 'WAIT')}"
        )
    else:
        warnings.append(f"REAL_5M_MTF_UNAVAILABLE:{mtf.get('reason', 'WAIT')}")

    signal.update(
        {
            "score": new_score,
            "decision_score": new_score,
            "mtf_confirmed": confirmed,
            "mtf_bonus": new_bonus,
            "real_mtf_5m": dict(mtf),
            "mtf_timeframe": "5m",
            "fake_mtf_bonus_removed": old_bonus,
            "warnings": list(dict.fromkeys(warnings)),
            "safety_gate_reasons": _clean_dynamic_reasons(
                signal.get("safety_gate_reasons")
            ),
            "real_mtf_patch": PATCH_VERSION,
        }
    )
    _update_breakdown(signal, old_bonus, new_bonus, confirmed, mtf)

    # Re-run the existing mandatory VWAP/Supertrend/EMA normalizer with the
    # corrected decision score.  This does not invent a new entry rule.
    try:
        from bot.mandatory_trend_structure_patch import _normalize

        signal = _normalize(signal, market)
    except Exception:
        minimum = _i(signal.get("min_score"), 82)
        signal["trade_allowed"] = bool(
            candidate in {"CE", "PE"}
            and new_score >= minimum
            and signal.get("trade_allowed", False)
        )
        signal["signal"] = candidate if signal["trade_allowed"] else "WAIT"

    bias = _session_bias(market, mtf)
    conflict = bool(
        bias in {"CE", "PE"}
        and candidate in {"CE", "PE"}
        and candidate != bias
    )
    reasons = _clean_dynamic_reasons(signal.get("safety_gate_reasons"))
    if conflict:
        reasons.append(f"SESSION_COUNTER_TREND_BLOCKED:{bias}")
        signal["trade_allowed"] = False
        signal["signal"] = "WAIT"

    signal["session_bias"] = bias
    signal["session_counter_trend_blocked"] = conflict
    signal["safety_gate_reasons"] = list(dict.fromkeys(reasons))
    signal["safety_gate_passed"] = bool(signal.get("trade_allowed", False))
    signal["score"] = new_score
    signal["decision_score"] = new_score

    payload = signal.get("live_score_breakdown")
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["score"] = new_score
        payload["decision_score"] = new_score
        payload["real_mtf_5m"] = dict(mtf)
        payload["session_bias"] = bias
        payload["session_counter_trend_blocked"] = conflict
        signal["live_score_breakdown"] = payload

    market["signal"] = signal.get("signal", "WAIT")
    market["signal_score"] = new_score
    market["session_bias"] = bias
    market["session_counter_trend_blocked"] = conflict

    scan["signal_data"] = signal
    scan["market_data"] = market
    scan["score"] = new_score
    scan["decision_score"] = new_score
    scan["real_mtf_5m"] = dict(mtf)
    scan["session_bias"] = bias
    scan["session_counter_trend_blocked"] = conflict
    scan["real_mtf_patch"] = PATCH_VERSION
    return scan


def _restore_normal_auto_cutoff() -> None:
    """Normal AUTO stops at 14:45; EOD remains 15:25; Hero Zero is separate."""
    try:
        from bot import eod_safety_testing_access_patch as eod
        from bot import paper_market_close_1530_patch as paper

        paper.PAPER_ENTRY_CUTOFF_MINUTE = NORMAL_AUTO_CUTOFF_MINUTE
        paper.LIVE_ENTRY_CUTOFF_MINUTE = NORMAL_AUTO_CUTOFF_MINUTE

        def window_labels(_mode: str) -> tuple[str, str]:
            return "09:15-14:45", "15:25"

        def entry_block_reason(value) -> str:
            if value.weekday() >= 5:
                return "AUTO_ENTRY_BLOCKED_MARKET_CLOSED"
            if paper._minute_of_day(value) < paper.ENTRY_START_MINUTE:
                return "AUTO_ENTRY_BLOCKED_BEFORE_0915_IST"
            return "AUTO_ENTRY_CUTOFF_1445_IST"

        paper._window_labels = window_labels
        paper._entry_block_reason = entry_block_reason
        eod.AUTO_ENTRY_CUTOFF_MINUTE = NORMAL_AUTO_CUTOFF_MINUTE
        eod._entry_window_open = paper._entry_window_open
        eod._entry_block_reason = entry_block_reason
        eod._mark_entry_time_block = paper._mark_entry_time_block
        eod._clear_entry_time_block = paper._clear_entry_time_block

        try:
            from bot import trade_miss_audit_patch as audit

            audit.ENTRY_CUTOFF_HOUR = 14
            audit.ENTRY_CUTOFF_MINUTE = 45
        except Exception:
            pass
    except Exception:
        pass


def apply_real_mtf_session_guard_patch() -> None:
    if getattr(runtime, "_okai_real_mtf_session_guard_v1", False):
        _restore_normal_auto_cutoff()
        return

    _restore_normal_auto_cutoff()

    original_build_scan = runtime._build_scan

    def build_scan_with_real_mtf(user_id, underlying, frame, profile, loss_streak):
        scan = original_build_scan(user_id, underlying, frame, profile, loss_streak)
        return _repair_scan(scan, frame, profile or {})

    runtime._build_scan = build_scan_with_real_mtf

    try:
        from bot import live_scan_history_fallback_patch as replay

        original_replay_scan = replay._replay_scan

        def replay_scan_with_real_mtf(
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
            return _repair_scan(scan, frame, profile or {})

        replay._replay_scan = replay_scan_with_real_mtf
        replay._okai_real_mtf_session_guard_v1 = True
    except Exception:
        pass

    original_summary = runtime._summary

    def summary_with_real_mtf(scan):
        data = original_summary(scan)
        if not isinstance(data, dict) or not isinstance(scan, dict):
            return data
        signal = scan.get("signal_data") or {}
        data["score"] = _i(signal.get("score", data.get("score", 0)), 0)
        data["decision_score"] = data["score"]
        data["real_mtf_5m"] = signal.get("real_mtf_5m")
        data["mtf_confirmed"] = bool(signal.get("mtf_confirmed", False))
        data["mtf_timeframe"] = "5m"
        data["session_bias"] = signal.get("session_bias", "NEUTRAL")
        data["session_counter_trend_blocked"] = bool(
            signal.get("session_counter_trend_blocked", False)
        )
        data["safety_gate_reasons"] = list(
            signal.get("safety_gate_reasons") or []
        )[:10]
        data["trade_allowed"] = bool(signal.get("trade_allowed", False))
        data["signal"] = signal.get("signal", "WAIT")
        if data["session_counter_trend_blocked"]:
            data["status"] = "SAFETY_BLOCKED"
            data["entry_status"] = "SAFETY_BLOCKED"
            data["entry_block_reason"] = next(
                (
                    reason
                    for reason in data["safety_gate_reasons"]
                    if str(reason).startswith("SESSION_COUNTER_TREND_BLOCKED")
                ),
                "SESSION_COUNTER_TREND_BLOCKED",
            )
        return data

    runtime._summary = summary_with_real_mtf
    runtime._okai_real_mtf_session_guard_v1 = True
