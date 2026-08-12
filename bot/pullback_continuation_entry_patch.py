"""Stateful pullback-continuation entry for mature directional moves.

The normal strategy must still qualify first.  When a fully aligned setup is
already too extended to buy safely, this patch remembers it instead of either
chasing immediately or forgetting it on the next scan:

``ARMED -> PULLBACK_SEEN -> CONTINUATION_READY``

Only the two exhaustion reasons that the pullback explicitly repairs are
released at the final transition.  Score 82, completed real 5-minute MTF,
VWAP/Supertrend/EMA structure, ORB direction, current EMA/VWAP anti-chase,
session cutoff, cooldown, sizing, option-contract and execution guards remain
mandatory and unchanged.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from database import get_db
from bot import auto_portfolio_runtime as runtime


PATCH_VERSION = "PULLBACK_CONTINUATION_ENTRY_V1"
STATE_ARMED = "ARMED"
STATE_PULLBACK = "PULLBACK_SEEN"
STATE_READY = "CONTINUATION_READY"

ARM_EXPIRY_MINUTES = 45
PULLBACK_EXPIRY_MINUTES = 6
READY_EXPIRY_MINUTES = 3
PULLBACK_CLOSE_MAX_ATR = 0.70
PULLBACK_TOUCH_TOLERANCE_ATR = 0.25
CONTINUATION_EMA_MAX_ATR = 0.95

RESOLVED_BY_PULLBACK = {
    "ORB_EXTENSION_OVER_1.35_ATR",
    "LATE_TWO_CANDLE_EXHAUSTION",
}
PULLBACK_STATUS_REASONS = {
    "PULLBACK_ENTRY_ARMED_WAITING_FOR_EMA",
    "PULLBACK_SEEN_WAITING_FOR_CONTINUATION",
}

_lock = threading.RLock()
_schema_ready = False


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _unique(values: Any) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _lock:
        if _schema_ready:
            return
        conn = get_db()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pullback_entry_states_v1(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  underlying TEXT NOT NULL,
                  side TEXT NOT NULL,
                  status TEXT NOT NULL,
                  armed_at TEXT NOT NULL,
                  armed_candle_id TEXT NOT NULL,
                  armed_price REAL NOT NULL,
                  armed_score INTEGER NOT NULL,
                  pullback_at TEXT,
                  pullback_candle_id TEXT,
                  pullback_price REAL,
                  ready_at TEXT,
                  ready_candle_id TEXT,
                  expires_at TEXT NOT NULL,
                  last_candle_id TEXT,
                  updated_at TEXT NOT NULL,
                  version TEXT NOT NULL,
                  UNIQUE(user_id,underlying)
                );
                CREATE INDEX IF NOT EXISTS idx_pullback_entry_expiry_v1
                ON pullback_entry_states_v1(expires_at);
                """
            )
            conn.commit()
            _schema_ready = True
        finally:
            conn.close()


def _load_state(user_id: int, underlying: str) -> dict[str, Any] | None:
    _ensure_schema()
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT * FROM pullback_entry_states_v1
               WHERE user_id=? AND underlying=?""",
            (int(user_id), str(underlying).upper()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _save_state(state: dict[str, Any]) -> dict[str, Any]:
    _ensure_schema()
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO pullback_entry_states_v1(
              user_id,underlying,side,status,armed_at,armed_candle_id,
              armed_price,armed_score,pullback_at,pullback_candle_id,
              pullback_price,ready_at,ready_candle_id,expires_at,
              last_candle_id,updated_at,version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id,underlying) DO UPDATE SET
              side=excluded.side,status=excluded.status,
              armed_at=excluded.armed_at,
              armed_candle_id=excluded.armed_candle_id,
              armed_price=excluded.armed_price,
              armed_score=excluded.armed_score,
              pullback_at=excluded.pullback_at,
              pullback_candle_id=excluded.pullback_candle_id,
              pullback_price=excluded.pullback_price,
              ready_at=excluded.ready_at,
              ready_candle_id=excluded.ready_candle_id,
              expires_at=excluded.expires_at,
              last_candle_id=excluded.last_candle_id,
              updated_at=excluded.updated_at,
              version=excluded.version
            """,
            (
                int(state["user_id"]),
                str(state["underlying"]).upper(),
                state["side"],
                state["status"],
                state["armed_at"],
                state["armed_candle_id"],
                _f(state["armed_price"]),
                _i(state["armed_score"]),
                state.get("pullback_at"),
                state.get("pullback_candle_id"),
                state.get("pullback_price"),
                state.get("ready_at"),
                state.get("ready_candle_id"),
                state["expires_at"],
                state.get("last_candle_id"),
                state["updated_at"],
                PATCH_VERSION,
            ),
        )
        conn.commit()
        row = conn.execute(
            """SELECT * FROM pullback_entry_states_v1
               WHERE user_id=? AND underlying=?""",
            (int(state["user_id"]), str(state["underlying"]).upper()),
        ).fetchone()
        return dict(row) if row else dict(state)
    finally:
        conn.close()


def _delete_state(user_id: int, underlying: str) -> None:
    _ensure_schema()
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM pullback_entry_states_v1 WHERE user_id=? AND underlying=?",
            (int(user_id), str(underlying).upper()),
        )
        conn.commit()
    finally:
        conn.close()


def _completed_candles(frame: Any, scan: dict[str, Any]) -> dict[str, Any]:
    market = dict(scan.get("market_data") or {})
    current = {
        "id": str(scan.get("candle_id") or ""),
        "open": _f(market.get("price")),
        "high": _f(market.get("price")),
        "low": _f(market.get("price")),
        "close": _f(market.get("price")),
        "bullish": bool(market.get("c2_bullish", False)),
    }
    previous = {
        "id": "",
        "bullish": bool(market.get("c1_bullish", False)),
    }
    try:
        if frame is not None and len(frame) >= 3:
            c2 = frame.iloc[-2]
            c1 = frame.iloc[-3]
            current = {
                "id": str(c2.get("time", current["id"])),
                "open": _f(c2.get("open"), current["open"]),
                "high": _f(c2.get("high"), current["high"]),
                "low": _f(c2.get("low"), current["low"]),
                "close": _f(c2.get("close"), current["close"]),
                "bullish": _f(c2.get("close")) > _f(c2.get("open")),
            }
            previous = {
                "id": str(c1.get("time", "")),
                "open": _f(c1.get("open")),
                "high": _f(c1.get("high")),
                "low": _f(c1.get("low")),
                "close": _f(c1.get("close")),
                "bullish": _f(c1.get("close")) > _f(c1.get("open")),
            }
    except Exception:
        pass
    return {"current": current, "previous": previous}


def _direction_checks(side: str, market: dict[str, Any]) -> dict[str, bool]:
    price = _f(market.get("price"))
    vwap = _f(market.get("vwap"), price)
    ema9 = _f(market.get("ema9"), price)
    ema21 = _f(market.get("ema21"), price)
    orb_high = _f(market.get("orb_high"))
    orb_low = _f(market.get("orb_low"))
    supertrend = str(market.get("supertrend_dir") or "NEUTRAL").upper()
    trend = str(market.get("trend") or "SIDEWAYS").upper()
    c1_bull = bool(market.get("c1_bullish", False))
    c2_bull = bool(market.get("c2_bullish", False))

    if side == "CE":
        return {
            "vwap": price > vwap,
            "supertrend": supertrend == "UP",
            "ema_trend": ema9 > ema21 and trend == "UPTREND",
            "orb": orb_high > 0 and price > orb_high + 5,
            "momentum": c1_bull and c2_bull,
        }
    if side == "PE":
        return {
            "vwap": price < vwap,
            "supertrend": supertrend == "DOWN",
            "ema_trend": ema9 < ema21 and trend == "DOWNTREND",
            "orb": orb_low > 0 and price < orb_low - 5,
            "momentum": (not c1_bull) and (not c2_bull),
        }
    return {key: False for key in ("vwap", "supertrend", "ema_trend", "orb", "momentum")}


def _reason_is_armable(reason: str) -> bool:
    text = str(reason or "").strip().upper()
    if not text:
        return True
    if text in RESOLVED_BY_PULLBACK:
        return True
    return text.startswith((
        "EMA_ANTI_CHASE",
        "EMA_EXTENSION_OVER_0.95_ATR",
        "CHASE_GUARD_BLOCKED",
    ))


def _armable_setup(
    signal: dict[str, Any],
    market: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> bool:
    side = str(signal.get("candidate_signal") or "WAIT").upper()
    score = _i(signal.get("score"))
    minimum = _i(signal.get("min_score"), 82)
    profile_key = str(
        (profile or {}).get("profile_key")
        or signal.get("strategy_profile_key")
        or "okai_default_82"
    )
    checks = _direction_checks(side, market)
    reasons = _unique(signal.get("safety_gate_reasons"))
    has_extension = any(
        reason in RESOLVED_BY_PULLBACK or reason.upper().startswith("EMA_ANTI_CHASE")
        for reason in reasons
    )
    mtf = dict(signal.get("real_mtf_5m") or {})
    mtf_ok = bool(
        signal.get("mtf_confirmed", False)
        and mtf.get("available", False)
        and str(mtf.get("side") or "WAIT").upper() == side
    )
    return bool(
        profile_key == "okai_default_82"
        and side in {"CE", "PE"}
        and score >= minimum
        and mtf_ok
        and all(checks.values())
        and has_extension
        and reasons
        and all(_reason_is_armable(reason) for reason in reasons)
        and not signal.get("session_counter_trend_blocked", False)
        and signal.get("entry_window_open", True) is not False
    )


def _pullback_seen(
    side: str,
    market: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    ema9 = _f(market.get("ema9"), _f(current.get("close")))
    atr = max(0.01, _f(market.get("atr"), 0.01))
    close_near = abs(_f(current.get("close")) - ema9) <= PULLBACK_CLOSE_MAX_ATR * atr
    if side == "PE":
        range_near = _f(current.get("high")) >= ema9 - PULLBACK_TOUCH_TOLERANCE_ATR * atr
        counter_candle = bool(current.get("bullish", False))
    else:
        range_near = _f(current.get("low")) <= ema9 + PULLBACK_TOUCH_TOLERANCE_ATR * atr
        counter_candle = not bool(current.get("bullish", True))
    return bool(counter_candle and (close_near or range_near))


def _continuation_ready(
    state: dict[str, Any],
    signal: dict[str, Any],
    market: dict[str, Any],
    candles: dict[str, Any],
    now: datetime,
) -> bool:
    side = str(state.get("side") or "WAIT").upper()
    current = candles["current"]
    price = _f(market.get("price"), _f(current.get("close")))
    ema9 = _f(market.get("ema9"), price)
    atr = max(0.01, _f(market.get("atr"), 0.01))
    checks = _direction_checks(side, market)
    mtf = dict(signal.get("real_mtf_5m") or {})
    pullback_at = _parse_time(state.get("pullback_at"))
    pullback_fresh = bool(
        pullback_at
        and now - pullback_at <= timedelta(minutes=PULLBACK_EXPIRY_MINUTES)
    )
    current_candle_id = str(current.get("id") or "")
    pullback_candle_id = str(state.get("pullback_candle_id") or "")
    current_candle_time = _parse_time(current_candle_id)
    pullback_candle_time = _parse_time(pullback_candle_id)
    candle_is_after_pullback = bool(
        current_candle_id
        and pullback_candle_id
        and current_candle_id != pullback_candle_id
        and (
            not current_candle_time
            or not pullback_candle_time
            or current_candle_time > pullback_candle_time
        )
    )
    if side == "PE":
        candle_aligned = not bool(current.get("bullish", True))
        resumed = price < _f(state.get("pullback_price"), price)
        correct_ema_side = price < ema9
    else:
        candle_aligned = bool(current.get("bullish", False))
        resumed = price > _f(state.get("pullback_price"), price)
        correct_ema_side = price > ema9

    current_anti_chase_clear = not bool(
        signal.get("ema_chase_blocked", False)
        or signal.get("vwap_chase_blocked", False)
        or signal.get("chase_blocked", False)
        or signal.get("sideways_blocked", False)
    )
    return bool(
        pullback_fresh
        and candle_is_after_pullback
        and candle_aligned
        and resumed
        and correct_ema_side
        and abs(price - ema9) / atr <= CONTINUATION_EMA_MAX_ATR
        and all(checks[key] for key in ("vwap", "supertrend", "ema_trend", "orb"))
        and _i(signal.get("score")) >= _i(signal.get("min_score"), 82)
        and signal.get("mtf_confirmed", False)
        and mtf.get("available", False)
        and str(mtf.get("side") or "WAIT").upper() == side
        and current_anti_chase_clear
        and not signal.get("session_counter_trend_blocked", False)
        and signal.get("entry_window_open", True) is not False
    )


def _remove_resolved(values: Any) -> list[str]:
    return [
        value
        for value in _unique(values)
        if value not in RESOLVED_BY_PULLBACK and value not in PULLBACK_STATUS_REASONS
    ]


def _remove_resolved_warnings(values: Any) -> list[str]:
    output = []
    for value in _unique(values):
        upper = value.upper()
        if any(reason in upper for reason in RESOLVED_BY_PULLBACK):
            continue
        if upper.startswith("PULLBACK_ENTRY_") or upper.startswith("PULLBACK_SEEN_"):
            continue
        output.append(value)
    return output


def _annotate_waiting(
    scan: dict[str, Any],
    state: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    signal = dict(scan.get("signal_data") or {})
    market = dict(scan.get("market_data") or {})
    reasons = [reason] + [
        value
        for value in _unique(signal.get("safety_gate_reasons"))
        if value not in PULLBACK_STATUS_REASONS
    ]
    warnings = _unique(signal.get("warnings"))
    marker = (
        "PULLBACK_ENTRY_ARMED:WAIT_FOR_EMA_PULLBACK"
        if state.get("status") == STATE_ARMED
        else "PULLBACK_SEEN:WAIT_FOR_CONTINUATION_CANDLE"
    )
    warnings = [marker] + [value for value in warnings if not value.startswith("PULLBACK_")]
    signal.update({
        "signal": "WAIT",
        "trade_allowed": False,
        "safety_gate_passed": False,
        "safety_gate_reasons": list(dict.fromkeys(reasons)),
        "warnings": warnings,
        "pullback_entry_mode": True,
        "pullback_entry_version": PATCH_VERSION,
        "pullback_entry_state": state.get("status"),
        "pullback_entry_state_id": state.get("id"),
        "pullback_entry_user_id": state.get("user_id"),
        "pullback_entry_side": state.get("side"),
        "pullback_entry_reason": reason,
        "pullback_entry_armed_at": state.get("armed_at"),
        "pullback_entry_pullback_at": state.get("pullback_at"),
        "pullback_entry_expires_at": state.get("expires_at"),
        "pullback_entry_ready": False,
    })
    signal["execution_allowed"] = False
    payload = signal.get("live_score_breakdown")
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.update({
            "trade_allowed": False,
            "execution_allowed": False,
            "pullback_entry_state": state.get("status"),
            "pullback_entry_reason": reason,
            "pullback_entry_ready": False,
        })
        signal["live_score_breakdown"] = payload
    market.update({
        "signal": "WAIT",
        "execution_allowed": False,
        "pullback_entry_state": state.get("status"),
        "pullback_entry_reason": reason,
    })
    scan["signal_data"] = signal
    scan["market_data"] = market
    scan["execution_allowed"] = False
    scan["pullback_entry_state"] = state.get("status")
    scan["pullback_entry_reason"] = reason
    return scan


def _release_continuation(
    scan: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    signal = dict(scan.get("signal_data") or {})
    market = dict(scan.get("market_data") or {})
    side = str(state.get("side") or "WAIT").upper()
    safety = _remove_resolved(signal.get("safety_gate_reasons"))
    fresh = _remove_resolved(signal.get("fresh_entry_block_reasons"))
    warnings = _remove_resolved_warnings(signal.get("warnings"))
    score_ok = _i(signal.get("score")) >= _i(signal.get("min_score"), 82)
    mtf = dict(signal.get("real_mtf_5m") or {})
    checks = _direction_checks(side, market)
    no_current_guard = not bool(
        signal.get("ema_chase_blocked", False)
        or signal.get("vwap_chase_blocked", False)
        or signal.get("chase_blocked", False)
        or signal.get("sideways_blocked", False)
        or signal.get("session_counter_trend_blocked", False)
    )
    eligible = bool(
        side in {"CE", "PE"}
        and score_ok
        and signal.get("mtf_confirmed", False)
        and mtf.get("available", False)
        and str(mtf.get("side") or "WAIT").upper() == side
        and all(checks[key] for key in ("vwap", "supertrend", "ema_trend", "orb"))
        and no_current_guard
        and not safety
        and not fresh
        and signal.get("entry_window_open", True) is not False
    )
    if not eligible:
        return _annotate_waiting(
            scan,
            state,
            "PULLBACK_SEEN_WAITING_FOR_CONTINUATION",
        )

    trigger_checks = dict(signal.get("fresh_trigger_checks") or {})
    trigger_checks["pullback_continuation"] = True
    warnings = ["PULLBACK_CONTINUATION_ENTRY_READY"] + warnings
    signal.update({
        "signal": side,
        "candidate_signal": side,
        "trade_allowed": True,
        "strategy_qualified": True,
        "safety_gate_passed": True,
        "safety_gate_reasons": [],
        "fresh_entry_ok": not fresh,
        "fresh_entry_block_reasons": fresh,
        "fresh_trigger_checks": trigger_checks,
        "fresh_trigger_passed": True,
        "fresh_trigger_required": "ORB_OR_MOMENTUM_OR_ARMED_PULLBACK_CONTINUATION",
        "entry_timing_blocked": False,
        "entry_timing_block_reasons": [],
        "entry_timing_mode": "STATEFUL_PULLBACK_CONTINUATION_V1",
        "warnings": list(dict.fromkeys(warnings)),
        "pullback_entry_mode": True,
        "pullback_entry_version": PATCH_VERSION,
        "pullback_entry_state": STATE_READY,
        "pullback_entry_state_id": state.get("id"),
        "pullback_entry_user_id": state.get("user_id"),
        "pullback_entry_side": side,
        "pullback_entry_reason": "PULLBACK_CONTINUATION_CONFIRMED",
        "pullback_entry_armed_at": state.get("armed_at"),
        "pullback_entry_pullback_at": state.get("pullback_at"),
        "pullback_entry_ready": True,
        "execution_allowed": True,
        "execution_block_reason": "",
    })
    payload = signal.get("live_score_breakdown")
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.update({
            "trade_allowed": True,
            "execution_allowed": True,
            "execution_block_reason": "",
            "pullback_entry_state": STATE_READY,
            "pullback_entry_reason": "PULLBACK_CONTINUATION_CONFIRMED",
            "pullback_entry_ready": True,
        })
        signal["live_score_breakdown"] = payload
    market.update({
        "signal": side,
        "execution_allowed": True,
        "execution_block_reason": "",
        "pullback_entry_state": STATE_READY,
        "pullback_entry_reason": "PULLBACK_CONTINUATION_CONFIRMED",
    })
    scan["signal_data"] = signal
    scan["market_data"] = market
    scan["execution_allowed"] = True
    scan["execution_block_reason"] = ""
    scan["pullback_entry_state"] = STATE_READY
    scan["pullback_entry_reason"] = "PULLBACK_CONTINUATION_CONFIRMED"
    for candle in reversed(scan.get("chart_candles") or []):
        if not isinstance(candle, dict):
            continue
        if scan.get("candle_id") and str(candle.get("time")) != str(scan.get("candle_id")):
            continue
        candle.update({
            "signal": side,
            "trade_allowed": True,
            "pullback_entry_ready": True,
            "score_source": "LIVE_CANONICAL_PULLBACK_CONTINUATION",
        })
        break
    return scan


def _new_armed_state(
    user_id: int,
    underlying: str,
    side: str,
    signal: dict[str, Any],
    market: dict[str, Any],
    candle_id: str,
    now: datetime,
) -> dict[str, Any]:
    return {
        "user_id": int(user_id),
        "underlying": str(underlying).upper(),
        "side": side,
        "status": STATE_ARMED,
        "armed_at": _iso(now),
        "armed_candle_id": candle_id,
        "armed_price": _f(market.get("price")),
        "armed_score": _i(signal.get("score")),
        "pullback_at": None,
        "pullback_candle_id": None,
        "pullback_price": None,
        "ready_at": None,
        "ready_candle_id": None,
        "expires_at": _iso(now + timedelta(minutes=ARM_EXPIRY_MINUTES)),
        "last_candle_id": candle_id,
        "updated_at": _iso(now),
        "version": PATCH_VERSION,
    }


def _repair_scan(
    user_id: int,
    underlying: str,
    frame: Any,
    scan: Any,
    profile: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Any:
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan
    signal = dict(scan.get("signal_data") or {})
    market = dict(scan.get("market_data") or {})
    if not signal or not market:
        return scan

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    instrument = str(underlying or scan.get("underlying") or "").upper()
    candles = _completed_candles(frame, scan)
    current = candles["current"]
    candle_id = str(scan.get("candle_id") or current.get("id") or "")
    side = str(signal.get("candidate_signal") or "WAIT").upper()
    state = _load_state(user_id, instrument)

    if state:
        expiry = _parse_time(state.get("expires_at"))
        mtf = dict(signal.get("real_mtf_5m") or {})
        direction_invalid = bool(
            side in {"CE", "PE"} and side != str(state.get("side") or "").upper()
        )
        mtf_flipped = bool(
            mtf.get("available", False)
            and str(mtf.get("side") or "WAIT").upper() in {"CE", "PE"}
            and str(mtf.get("side") or "WAIT").upper() != str(state.get("side") or "").upper()
        )
        checks = _direction_checks(str(state.get("side") or "WAIT").upper(), market)
        structure_invalid = not all(checks[key] for key in ("vwap", "supertrend", "ema_trend", "orb"))
        if (
            not expiry
            or current_time >= expiry
            or direction_invalid
            or mtf_flipped
            or structure_invalid
            or signal.get("entry_window_open", True) is False
        ):
            _delete_state(user_id, instrument)
            state = None

    if state is None:
        if not _armable_setup(signal, market, profile):
            return scan
        state = _save_state(
            _new_armed_state(
                user_id,
                instrument,
                side,
                signal,
                market,
                candle_id,
                current_time,
            )
        )
        return _annotate_waiting(
            scan,
            state,
            "PULLBACK_ENTRY_ARMED_WAITING_FOR_EMA",
        )

    status = str(state.get("status") or STATE_ARMED)
    # Never make the original protected strategy stricter. If a later scan is
    # independently qualified by every existing gate, discard the remembered
    # fallback and let the normal entry path proceed unchanged.
    if (
        status != STATE_READY
        and signal.get("trade_allowed", False)
        and signal.get("execution_allowed", True) is not False
    ):
        _delete_state(user_id, instrument)
        return scan

    if status == STATE_ARMED and _pullback_seen(str(state["side"]), market, current):
        state.update({
            "status": STATE_PULLBACK,
            "pullback_at": _iso(current_time),
            "pullback_candle_id": candle_id,
            "pullback_price": _f(market.get("price"), _f(current.get("close"))),
            "expires_at": _iso(current_time + timedelta(minutes=PULLBACK_EXPIRY_MINUTES)),
            "last_candle_id": candle_id,
            "updated_at": _iso(current_time),
        })
        state = _save_state(state)
        return _annotate_waiting(
            scan,
            state,
            "PULLBACK_SEEN_WAITING_FOR_CONTINUATION",
        )

    if status == STATE_PULLBACK and _continuation_ready(
        state,
        signal,
        market,
        candles,
        current_time,
    ):
        state.update({
            "status": STATE_READY,
            "ready_at": _iso(current_time),
            "ready_candle_id": candle_id,
            "expires_at": _iso(current_time + timedelta(minutes=READY_EXPIRY_MINUTES)),
            "last_candle_id": candle_id,
            "updated_at": _iso(current_time),
        })
        state = _save_state(state)
        return _release_continuation(scan, state)

    if status == STATE_READY:
        ready_at = _parse_time(state.get("ready_at"))
        still_fresh = bool(
            ready_at and current_time - ready_at <= timedelta(minutes=READY_EXPIRY_MINUTES)
        )
        side_checks = _direction_checks(str(state.get("side") or "WAIT"), market)
        current_safe = bool(
            still_fresh
            and all(side_checks[key] for key in ("vwap", "supertrend", "ema_trend", "orb"))
            and not signal.get("ema_chase_blocked", False)
            and not signal.get("vwap_chase_blocked", False)
            and not signal.get("chase_blocked", False)
            and _i(signal.get("score")) >= _i(signal.get("min_score"), 82)
            and signal.get("mtf_confirmed", False)
        )
        if current_safe:
            return _release_continuation(scan, state)
        _delete_state(user_id, instrument)
        return scan

    state.update({
        "last_candle_id": candle_id,
        "updated_at": _iso(current_time),
    })
    state = _save_state(state)
    reason = (
        "PULLBACK_ENTRY_ARMED_WAITING_FOR_EMA"
        if status == STATE_ARMED
        else "PULLBACK_SEEN_WAITING_FOR_CONTINUATION"
    )
    return _annotate_waiting(scan, state, reason)


def apply_pullback_continuation_entry_patch() -> None:
    if getattr(runtime, "_okai_pullback_continuation_entry_v1", False):
        return

    original_build_scan = runtime._build_scan
    original_summary = runtime._summary
    original_attempt = runtime._attempt_entry_candidates

    def build_scan_with_pullback(user_id, underlying, frame, profile, loss_streak):
        scan = original_build_scan(user_id, underlying, frame, profile, loss_streak)
        return _repair_scan(user_id, underlying, frame, scan, profile or {})

    def summary_with_pullback(scan):
        data = dict(original_summary(scan) or {})
        if not isinstance(scan, dict):
            return data
        signal = dict(scan.get("signal_data") or {})
        state = str(signal.get("pullback_entry_state") or "")
        reason = str(signal.get("pullback_entry_reason") or "")
        data.update({
            "pullback_entry_mode": bool(signal.get("pullback_entry_mode", False)),
            "pullback_entry_state": state or None,
            "pullback_entry_reason": reason or None,
            "pullback_entry_ready": bool(signal.get("pullback_entry_ready", False)),
            "pullback_entry_side": signal.get("pullback_entry_side"),
            "pullback_entry_expires_at": signal.get("pullback_entry_expires_at"),
        })
        if state in {STATE_ARMED, STATE_PULLBACK}:
            data["status"] = "PULLBACK_WAIT"
            data["entry_status"] = state
            data["entry_block_reason"] = reason
            data["trade_allowed"] = False
            data["execution_allowed"] = False
            data["signal"] = "WAIT"
        elif state == STATE_READY and signal.get("trade_allowed", False):
            data["status"] = "QUALIFIED"
            data["entry_status"] = "PULLBACK_CONTINUATION_READY"
            data["entry_block_reason"] = None
            data["trade_allowed"] = True
            data["execution_allowed"] = bool(signal.get("execution_allowed", True))
            data["signal"] = signal.get("signal", "WAIT")
        return data

    def attempt_with_pullback_cleanup(candidates, opener, engine_state):
        opened = original_attempt(candidates, opener, engine_state)
        if not isinstance(opened, dict):
            return opened
        signal = dict(opened.get("signal_data") or {})
        if signal.get("pullback_entry_ready", False):
            user_id = _i(signal.get("pullback_entry_user_id"), 0)
            state_id = _i(signal.get("pullback_entry_state_id"), 0)
            underlying = str(opened.get("underlying") or "").upper()
            if user_id > 0 and underlying:
                _delete_state(user_id, underlying)
            elif state_id > 0:
                _ensure_schema()
                conn = get_db()
                try:
                    conn.execute(
                        "DELETE FROM pullback_entry_states_v1 WHERE id=?",
                        (state_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()
        return opened

    runtime._build_scan = build_scan_with_pullback
    runtime._summary = summary_with_pullback
    runtime._attempt_entry_candidates = attempt_with_pullback_cleanup
    runtime._okai_pullback_continuation_entry_v1 = True
