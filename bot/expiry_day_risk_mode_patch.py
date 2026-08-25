"""Final expiry-day entry, sizing and loss-control authority.

Normal-day AUTO rules remain unchanged.  On a same-day index expiry this layer
requires a stronger completed-candle setup, stops normal entries before the
late-expiry noise window, sizes lots by planned SL loss, and limits repeated
expiry losses.  ORB remains telemetry rather than a hard requirement.
"""

from __future__ import annotations

import math
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

from bot import auto_portfolio_runtime as runtime


VERSION = "OKAI-EXPIRY-DAY-RISK-MODE-V1"
EXPIRY_MIN_SCORE = 88
EXPIRY_ENTRY_START_MINUTE = 9 * 60 + 30
EXPIRY_ENTRY_CUTOFF_MINUTE = 14 * 60 + 45
EXPIRY_MAX_PLANNED_LOSS_PERCENT = 1.0
EXPIRY_DAILY_LOSS_LIMIT_PERCENT = 2.0
EXPIRY_MAX_TRADES_PER_DAY = 2
EXPIRY_COOLDOWN_MINUTES = 30
EXPIRY_MAX_SAME_SIDE_SL_LOSSES = 2
EXPIRY_PREMIUM_DROP_POINTS = 0.10
EXPIRY_PREMIUM_DROP_PERCENT = 0.25
EXPIRY_PREMIUM_SAMPLE_SECONDS = 180

_context = threading.local()
_quote_lock = threading.RLock()
_last_quotes: dict[tuple[int, str], tuple[datetime, float]] = {}
IST = timezone(timedelta(hours=5, minutes=30))


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


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_expiry(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip().upper()
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    return None


def _last_tuesday(value: date) -> bool:
    return value.weekday() == 1 and (value + timedelta(days=7)).month != value.month


def _scan_is_expiry_session(underlying: Any, value: datetime | None = None) -> bool:
    now = value or _now_ist()
    name = str(underlying or "").upper()
    if name == "NIFTY":
        return now.weekday() == 1
    if name == "BANKNIFTY":
        return _last_tuesday(now.date())
    return False


def _resolved_expires_today(resolved: Any, value: datetime | None = None) -> bool:
    if not isinstance(resolved, dict):
        return False
    now = value or _now_ist()
    expiry = _parse_expiry(
        resolved.get("expiry_date") or resolved.get("expiry")
    )
    return expiry == now.date()


def _entry_is_expiry_session(
    selected: dict[str, Any],
    resolved: dict[str, Any],
    value: datetime,
) -> bool:
    underlying = str((selected or {}).get("underlying") or "").upper()
    return bool(
        _resolved_expires_today(resolved, value)
        or (selected or {}).get("expiry_day_mode")
        or _scan_is_expiry_session(underlying, value)
    )


def _candidate(signal: dict[str, Any]) -> str:
    side = str(
        signal.get("candidate_signal") or signal.get("signal") or "WAIT"
    ).upper()
    return side if side in {"CE", "PE"} else "WAIT"


def _index_momentum(side: str, market: dict[str, Any]) -> bool:
    c1 = bool(market.get("c1_bullish", False))
    c2 = bool(market.get("c2_bullish", False))
    if side == "CE":
        return c1 and c2
    if side == "PE":
        return not c1 and not c2
    return False


def _real_5m_matches(side: str, signal: dict[str, Any], scan: dict[str, Any]) -> bool:
    snapshot = (
        signal.get("real_mtf_5m")
        or scan.get("real_mtf_5m")
        or {}
    )
    return bool(
        isinstance(snapshot, dict)
        and snapshot.get("available")
        and str(snapshot.get("side") or "WAIT").upper() == side
    )


def _expiry_window_open(value: datetime | None = None) -> bool:
    now = value or _now_ist()
    minute = now.hour * 60 + now.minute
    return bool(
        now.weekday() < 5
        and EXPIRY_ENTRY_START_MINUTE <= minute < EXPIRY_ENTRY_CUTOFF_MINUTE
    )


def _add_reason(values: Any, reason: str) -> list[str]:
    result = [str(value) for value in (values or []) if str(value).strip()]
    if reason not in result:
        result.append(reason)
    return result


def _apply_scan_guard(scan: Any) -> Any:
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan
    if not _scan_is_expiry_session(scan.get("underlying")):
        return scan

    signal = dict(scan.get("signal_data") or {})
    market = dict(scan.get("market_data") or {})
    if not signal or not market:
        return scan

    side = _candidate(signal)
    score = _i(signal.get("score"), 0)
    score_ok = score >= EXPIRY_MIN_SCORE
    momentum_ok = _index_momentum(side, market)
    mtf_ok = _real_5m_matches(side, signal, scan)
    window_ok = _expiry_window_open()
    upstream_allowed = bool(signal.get("trade_allowed", False))

    reasons = list(signal.get("safety_gate_reasons") or [])
    if not score_ok:
        reasons = _add_reason(reasons, f"EXPIRY_SCORE_BELOW_{EXPIRY_MIN_SCORE}")
    if not momentum_ok:
        reasons = _add_reason(reasons, "EXPIRY_TWO_CANDLE_MOMENTUM_REQUIRED")
    if not mtf_ok:
        reasons = _add_reason(reasons, "EXPIRY_REAL_5M_DIRECTION_REQUIRED")
    if not window_ok:
        reasons = _add_reason(reasons, "EXPIRY_AUTO_ENTRY_WINDOW_0930_1445_IST")

    allowed = bool(
        upstream_allowed
        and side in {"CE", "PE"}
        and score_ok
        and momentum_ok
        and mtf_ok
        and window_ok
    )
    signal.update({
        "signal": side if allowed else "WAIT",
        "candidate_signal": side,
        "score": score,
        "decision_score": score,
        "min_score": EXPIRY_MIN_SCORE,
        "min_score_required": EXPIRY_MIN_SCORE,
        "trade_allowed": allowed,
        "strategy_qualified": allowed,
        "execution_allowed": allowed,
        "safety_gate_passed": allowed,
        "safety_gate_reasons": reasons,
        "expiry_day_mode": True,
        "expiry_day_rule_version": VERSION,
        "expiry_score_required": EXPIRY_MIN_SCORE,
        "expiry_two_candle_momentum_required": True,
        "expiry_real_5m_required": True,
        "expiry_orb_required": False,
        "expiry_entry_window_ist": "09:30-14:45",
    })
    if not allowed:
        signal["execution_block_reason"] = reasons[-1] if reasons else "EXPIRY_ENTRY_BLOCKED"

    market.update({
        "signal": signal["signal"],
        "signal_score": score,
        "signal_min_score": EXPIRY_MIN_SCORE,
        "expiry_day_mode": True,
    })
    scan.update({
        "signal_data": signal,
        "market_data": market,
        "score": score,
        "decision_score": score,
        "expiry_day_mode": True,
        "expiry_day_rule_version": VERSION,
    })
    return scan


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS expiry_day_loss_blocks_v1 (
            user_id INTEGER NOT NULL,
            underlying TEXT NOT NULL,
            side TEXT NOT NULL,
            trade_day TEXT NOT NULL,
            loss_count INTEGER NOT NULL DEFAULT 0,
            blocked_until TEXT,
            source_trade_id INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, underlying, side, trade_day)
        )
        """
    )
    conn.commit()


def _day_bounds_utc(now_ist: datetime) -> tuple[str, str]:
    local = (
        now_ist.replace(tzinfo=IST)
        if now_ist.tzinfo is None
        else now_ist.astimezone(IST)
    )
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


def _today_expiry_rows(conn, user_id: int, now_ist: datetime) -> list[Any]:
    start, end = _day_bounds_utc(now_ist)
    rows = conn.execute(
        """
        SELECT * FROM paper_trades
        WHERE user_id=?
          AND datetime(created_at)>=datetime(?)
          AND datetime(created_at)<datetime(?)
        ORDER BY id ASC
        """,
        (int(user_id), start, end),
    ).fetchall()
    return [row for row in rows if _parse_expiry(runtime._v(row, "expiry")) == now_ist.date()]


def _capital_base(conn, user_id: int, settings: dict, live_cash, rows: list[Any]) -> float:
    mode = str(settings.get("trading_mode", "paper") or "paper").lower()
    if mode == "live":
        return max(0.0, runtime._live_base_from_rows(rows) or _f(live_cash(), 0.0))
    return max(0.0, runtime._paper_base(conn, user_id, settings))


def _net(row: Any) -> float:
    value = runtime._v(row, "net_pnl")
    if value is None:
        value = runtime._v(row, "pnl", 0.0)
    return _f(value, 0.0)


def _active_loss_block(conn, user_id: int, underlying: str, side: str, now_ist: datetime) -> dict[str, Any] | None:
    _ensure_schema(conn)
    row = conn.execute(
        """
        SELECT * FROM expiry_day_loss_blocks_v1
        WHERE user_id=? AND underlying=? AND side=? AND trade_day=?
        """,
        (int(user_id), underlying, side, now_ist.date().isoformat()),
    ).fetchone()
    if not row:
        return None
    try:
        blocked_until = datetime.fromisoformat(str(row["blocked_until"]).replace("Z", "+00:00"))
        if blocked_until.tzinfo is None:
            blocked_until = blocked_until.replace(tzinfo=timezone.utc)
    except Exception:
        blocked_until = None
    now_utc = datetime.now(timezone.utc)
    if blocked_until and blocked_until > now_utc:
        return {
            "loss_count": _i(row["loss_count"], 0),
            "blocked_until": blocked_until.isoformat(),
            "remaining_seconds": max(1, int((blocked_until - now_utc).total_seconds())),
        }
    return None


def _premium_not_falling(user_id: int, symbol: str, price: float) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    key = (int(user_id), str(symbol or "").upper())
    current = max(0.0, _f(price, 0.0))
    with _quote_lock:
        previous = _last_quotes.get(key)
        _last_quotes[key] = (now, current)
    if not previous or (now - previous[0]).total_seconds() > EXPIRY_PREMIUM_SAMPLE_SECONDS:
        return {"allowed": True, "reason": "EXPIRY_PREMIUM_FIRST_SAMPLE", "current": current}
    previous_price = max(0.0, previous[1])
    drop = previous_price - current
    threshold = max(
        EXPIRY_PREMIUM_DROP_POINTS,
        previous_price * EXPIRY_PREMIUM_DROP_PERCENT / 100.0,
    )
    return {
        "allowed": drop < threshold - 1e-9,
        "reason": "EXPIRY_PREMIUM_NOT_FALLING" if drop < threshold - 1e-9 else "EXPIRY_PREMIUM_FALLING",
        "previous": round(previous_price, 2),
        "current": round(current, 2),
        "drop": round(drop, 2),
        "block_threshold": round(threshold, 2),
    }


def _record_block(state: dict, selected: dict, reason: str, details: dict[str, Any] | None = None) -> bool:
    signal = dict((selected or {}).get("signal_data") or {})
    payload = {
        "allowed": False,
        "reason": reason,
        "stage": "EXPIRY_DAY_RISK_MODE",
        "underlying": selected.get("underlying"),
        "side": signal.get("candidate_signal") or signal.get("signal"),
        "details": details or {},
        "version": VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    state["entry_guard"] = payload
    state["entry_attempt"] = payload
    state["last_entry_attempt"] = payload
    state["entry_block_reason"] = reason
    state["last_entry_block_reason"] = reason
    return False


def _cap_expiry_size(
    base: dict[str, Any],
    capital_base: float,
    premium: float,
    lot_size: int,
    risk_points: float,
) -> dict[str, Any]:
    output = dict(base or {})
    original_lots = max(0, _i(output.get("lots"), 0))
    risk = max(0.05, _f(risk_points, 0.05))
    lot = max(1, _i(lot_size, 1))
    risk_budget = max(
        0.0,
        _f(capital_base) * EXPIRY_MAX_PLANNED_LOSS_PERCENT / 100.0,
    )
    risk_per_lot = risk * lot
    risk_lots = (
        int(math.floor((risk_budget + 1e-9) / risk_per_lot))
        if risk_per_lot > 0
        else 0
    )
    allowed_lots = min(original_lots, risk_lots)
    qty = allowed_lots * lot
    output.update({
        "lots": allowed_lots,
        "qty": qty,
        "capital_used": round(max(0.0, _f(premium)) * qty, 2),
        "risk_cap_applied": allowed_lots < original_lots,
        "risk_sizing_mode": "EXPIRY_PLANNED_SL_LOSS_CAP_1PCT",
        "sizing_mode": "EXPIRY_PLANNED_SL_LOSS_CAP_1PCT",
        "expiry_day_mode": True,
        "max_planned_loss_percent": EXPIRY_MAX_PLANNED_LOSS_PERCENT,
        "max_planned_loss_amount": round(risk_budget, 2),
        "planned_risk_points": round(risk, 2),
        "planned_risk_per_lot": round(risk_per_lot, 2),
        "risk_lots": risk_lots,
        "actual_allocation_pct": round(
            (_f(premium) * qty / max(0.01, _f(capital_base))) * 100.0,
            2,
        ),
    })
    return output


def _register_expiry_loss(conn, user_id: int, trade: Any, price: float, reason: Any) -> None:
    expiry = _parse_expiry(runtime._v(trade, "expiry"))
    now_ist = _now_ist()
    if expiry != now_ist.date():
        return
    qty = max(1, _i(runtime._v(trade, "qty", 1), 1))
    pnl = runtime._v(trade, "net_pnl")
    if pnl is None:
        pnl = (_f(price) - _f(runtime._v(trade, "entry_price"))) * qty
    reason_text = str(reason or "").upper()
    if _f(pnl, 0.0) >= 0 or ("SL" not in reason_text and "LOSS" not in reason_text):
        return

    _ensure_schema(conn)
    underlying = str(runtime._underlying(trade) or "").upper()
    side = str(runtime._v(trade, "side", "") or "").upper()
    current = conn.execute(
        """
        SELECT loss_count FROM expiry_day_loss_blocks_v1
        WHERE user_id=? AND underlying=? AND side=? AND trade_day=?
        """,
        (int(user_id), underlying, side, now_ist.date().isoformat()),
    ).fetchone()
    loss_count = _i(current["loss_count"], 0) + 1 if current else 1
    if loss_count >= EXPIRY_MAX_SAME_SIDE_SL_LOSSES:
        local_now = (
            now_ist.replace(tzinfo=IST)
            if now_ist.tzinfo is None
            else now_ist.astimezone(IST)
        )
        next_day = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        blocked_until = next_day.astimezone(timezone.utc)
    else:
        blocked_until = datetime.now(timezone.utc) + timedelta(minutes=EXPIRY_COOLDOWN_MINUTES)
    conn.execute(
        """
        INSERT INTO expiry_day_loss_blocks_v1 (
            user_id, underlying, side, trade_day, loss_count,
            blocked_until, source_trade_id, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, underlying, side, trade_day) DO UPDATE SET
            loss_count=excluded.loss_count,
            blocked_until=excluded.blocked_until,
            source_trade_id=excluded.source_trade_id,
            updated_at=excluded.updated_at
        """,
        (
            int(user_id), underlying, side, now_ist.date().isoformat(), loss_count,
            blocked_until.isoformat(), _i(runtime._v(trade, "id", 0), 0),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def apply_expiry_day_risk_mode_patch() -> bool:
    if getattr(runtime, "_okai_expiry_day_risk_mode_v1", False):
        return True

    previous_build_scan = runtime._build_scan
    previous_open = runtime._open_common
    previous_close = runtime._close
    previous_size = runtime._size
    previous_ensure = runtime._ensure_schema

    def build_scan_with_expiry_guard(*args, **kwargs):
        return _apply_scan_guard(previous_build_scan(*args, **kwargs))

    def size_with_expiry_risk(
        capital_base,
        slot,
        premium,
        lot_size,
        rows=None,
        risk_points=None,
    ):
        base = dict(
            previous_size(
                capital_base,
                slot,
                premium,
                lot_size,
                rows=rows,
                risk_points=risk_points,
            )
            or {}
        )
        context = getattr(_context, "expiry", None)
        if not isinstance(context, dict):
            return base
        return _cap_expiry_size(
            base,
            capital_base,
            premium,
            lot_size,
            context.get("risk_points"),
        )

    def ensure_with_expiry_schema(conn):
        result = previous_ensure(conn)
        _ensure_schema(conn)
        return result

    def open_with_expiry_guard(
        conn, user_id, broker_name, selected, settings, resolved, quote_price,
        quality, lot_size, live_order, live_cash, state,
    ):
        now_ist = _now_ist()
        underlying = str(selected.get("underlying") or "").upper()
        resolved_today = _resolved_expires_today(resolved, now_ist)
        expiry_session = _entry_is_expiry_session(selected, resolved, now_ist)
        if not expiry_session:
            return previous_open(
                conn, user_id, broker_name, selected, settings, resolved,
                quote_price, quality, lot_size, live_order, live_cash, state,
            )
        if not resolved_today:
            return _record_block(
                state,
                selected,
                "EXPIRY_EXACT_SAME_DAY_CONTRACT_REQUIRED",
                {
                    "resolved_expiry": resolved.get("expiry_date") or resolved.get("expiry"),
                    "required_expiry": now_ist.date().isoformat(),
                },
            )

        signal = dict((selected or {}).get("signal_data") or {})
        side = _candidate(signal)
        if not _expiry_window_open(now_ist):
            return _record_block(state, selected, "EXPIRY_AUTO_ENTRY_WINDOW_0930_1445_IST")
        if _i(signal.get("score"), 0) < EXPIRY_MIN_SCORE:
            return _record_block(state, selected, f"EXPIRY_SCORE_BELOW_{EXPIRY_MIN_SCORE}")
        if not _index_momentum(side, dict(selected.get("market_data") or {})):
            return _record_block(state, selected, "EXPIRY_TWO_CANDLE_MOMENTUM_REQUIRED")
        if not _real_5m_matches(side, signal, selected):
            return _record_block(state, selected, "EXPIRY_REAL_5M_DIRECTION_REQUIRED")

        premium = _premium_not_falling(user_id, resolved.get("symbol"), quote_price)
        state["expiry_premium_confirmation"] = premium
        if not premium.get("allowed"):
            return _record_block(state, selected, "EXPIRY_PREMIUM_FALLING", premium)

        rows = runtime._open_rows(conn, user_id)
        mode = "live" if str(settings.get("trading_mode", "paper")).lower() == "live" else "paper"
        same_mode_open = [row for row in rows if runtime._mode(row) == mode]
        if same_mode_open:
            return _record_block(
                state, selected, "EXPIRY_MAX_ONE_OPEN_POSITION",
                {"open_trade_id": runtime._v(same_mode_open[0], "id")},
            )

        capital = _capital_base(conn, user_id, settings, live_cash, rows)
        today_rows = _today_expiry_rows(conn, user_id, now_ist)
        if len(today_rows) >= EXPIRY_MAX_TRADES_PER_DAY:
            return _record_block(
                state, selected, "EXPIRY_MAX_TRADES_PER_DAY_REACHED",
                {"today_expiry_trades": len(today_rows), "maximum": EXPIRY_MAX_TRADES_PER_DAY},
            )
        realized = sum(_net(row) for row in today_rows if str(runtime._v(row, "status", "")).upper() == "CLOSED")
        daily_limit = capital * EXPIRY_DAILY_LOSS_LIMIT_PERCENT / 100.0
        if realized <= -daily_limit + 1e-9:
            return _record_block(
                state, selected, "EXPIRY_DAILY_LOSS_LIMIT_REACHED",
                {"realized_net_pnl": round(realized, 2), "loss_limit": round(daily_limit, 2)},
            )

        loss_block = _active_loss_block(conn, user_id, underlying, side, now_ist)
        if loss_block:
            return _record_block(state, selected, "EXPIRY_POST_SL_COOLDOWN", loss_block)

        market = dict(selected.get("market_data") or {})
        levels = runtime._legacy()._dynamic_atr_levels(
            _f(market.get("price")),
            _f(quote_price),
            _f(market.get("atr")),
            is_expiry_day=True,
        )
        _context.expiry = {"risk_points": _f(levels.get("risk_points"), 0.05)}
        try:
            opened = previous_open(
                conn, user_id, broker_name, selected, settings, resolved,
                quote_price, quality, lot_size, live_order, live_cash, state,
            )
        finally:
            try:
                delattr(_context, "expiry")
            except Exception:
                pass
        state["expiry_day_risk_mode"] = {
            "active": True,
            "version": VERSION,
            "score_required": EXPIRY_MIN_SCORE,
            "entry_window_ist": "09:30-14:45",
            "max_planned_loss_percent": EXPIRY_MAX_PLANNED_LOSS_PERCENT,
            "daily_loss_limit_percent": EXPIRY_DAILY_LOSS_LIMIT_PERCENT,
            "max_trades_per_day": EXPIRY_MAX_TRADES_PER_DAY,
        }
        return opened

    def close_with_expiry_loss_control(conn, user_id, trade, price, reason, order_id=None):
        result = previous_close(conn, user_id, trade, price, reason, order_id)
        _register_expiry_loss(conn, user_id, trade, price, reason)
        return result

    runtime._build_scan = build_scan_with_expiry_guard
    runtime._size = size_with_expiry_risk
    runtime._ensure_schema = ensure_with_expiry_schema
    runtime._open_common = open_with_expiry_guard
    runtime._close = close_with_expiry_loss_control
    runtime._okai_expiry_day_risk_mode_v1 = True
    runtime._okai_expiry_day_risk_mode_version = VERSION
    return True


__all__ = [
    "EXPIRY_DAILY_LOSS_LIMIT_PERCENT",
    "EXPIRY_ENTRY_CUTOFF_MINUTE",
    "EXPIRY_MAX_PLANNED_LOSS_PERCENT",
    "EXPIRY_MIN_SCORE",
    "VERSION",
    "apply_expiry_day_risk_mode_patch",
]
