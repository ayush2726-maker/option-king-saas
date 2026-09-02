"""Loss-count circuit for repeated full ATR stops.

Policy:
* two full ATR stops within 60 minutes pause normal AUTO entries for 45 minutes;
* the third full ATR stop in the IST trading day blocks further normal AUTO
  entries for that day.

This is deliberately count-based.  It does not introduce a capital-risk
percentage or a daily monetary/percentage loss limit, and it does not affect
Hero Zero or manual orders.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bot import auto_portfolio_runtime as runtime


VERSION = "OKAI-FULL-SL-VELOCITY-CIRCUIT-V1"
WINDOW_SECONDS = 60 * 60
PAUSE_SECONDS = 45 * 60
DAY_STOP_COUNT = 3
PAUSE_REASON = "TWO_FULL_SL_WITHIN_60M_PAUSE_45M"
DAY_STOP_REASON = "THREE_FULL_SL_TRADING_DAY_STOP"


def _value(row: Any, key: str, default=None):
    try:
        if key in row.keys() and row[key] is not None:
            return row[key]
    except Exception:
        pass
    if isinstance(row, dict):
        value = row.get(key)
        return default if value is None else value
    return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _mode(settings_or_trade: Any) -> str:
    value = _value(settings_or_trade, "trading_mode", "paper")
    return "live" if str(value or "paper").lower() == "live" else "paper"


def _full_atr_stop(reason: Any) -> bool:
    text = str(reason or "").upper()
    return "PURE ATR SL" in text


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_full_sl_events (
            user_id INTEGER NOT NULL,
            trading_mode TEXT NOT NULL,
            source_trade_id INTEGER NOT NULL,
            trading_day_ist TEXT NOT NULL,
            underlying TEXT,
            side TEXT,
            reason TEXT,
            closed_at TEXT NOT NULL,
            PRIMARY KEY (user_id, trading_mode, source_trade_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_full_sl_events_day
        ON auto_full_sl_events (
            user_id, trading_mode, trading_day_ist, closed_at
        )
        """
    )
    conn.commit()


def _register_full_sl(
    conn,
    user_id: int,
    trade: Any,
    reason: Any,
    now: datetime | None = None,
) -> bool:
    if not _full_atr_stop(reason):
        return False
    trade_id = _i(_value(trade, "id", 0), 0)
    if trade_id <= 0:
        return False

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    trading_day = current.astimezone(
        timezone(timedelta(hours=5, minutes=30))
    ).date().isoformat()
    _ensure_schema(conn)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO auto_full_sl_events (
            user_id, trading_mode, source_trade_id, trading_day_ist,
            underlying, side, reason, closed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            _mode(trade),
            trade_id,
            trading_day,
            str(runtime._underlying(trade) or "").upper(),
            str(_value(trade, "side", "") or "").upper(),
            str(reason or "")[:240],
            _iso(current),
        ),
    )
    conn.commit()
    return bool(cursor.rowcount)


def _active_block(
    conn,
    user_id: int,
    mode: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    trading_day = current.astimezone(
        timezone(timedelta(hours=5, minutes=30))
    ).date().isoformat()
    _ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT source_trade_id, underlying, side, reason, closed_at
        FROM auto_full_sl_events
        WHERE user_id=? AND trading_mode=? AND trading_day_ist=?
        ORDER BY datetime(closed_at) ASC, source_trade_id ASC
        """,
        (int(user_id), str(mode), trading_day),
    ).fetchall()
    events = [
        (_parse(_value(row, "closed_at")), row)
        for row in rows
    ]
    events = [(closed, row) for closed, row in events if closed is not None]
    count = len(events)
    if count >= DAY_STOP_COUNT:
        return {
            "reason": DAY_STOP_REASON,
            "full_sl_count_today": count,
            "trading_day_ist": trading_day,
            "source_trade_id": _value(events[-1][1], "source_trade_id"),
        }

    if count >= 2:
        first, second = events[-2][0], events[-1][0]
        if (second - first).total_seconds() <= WINDOW_SECONDS:
            blocked_until = second + timedelta(seconds=PAUSE_SECONDS)
            if current < blocked_until:
                return {
                    "reason": PAUSE_REASON,
                    "full_sl_count_today": count,
                    "window_seconds": int((second - first).total_seconds()),
                    "blocked_until": _iso(blocked_until),
                    "remaining_seconds": max(
                        1, int((blocked_until - current).total_seconds())
                    ),
                    "source_trade_id": _value(events[-1][1], "source_trade_id"),
                }
    return None


def _set_block(state: dict | None, block: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        return
    payload = {
        "allowed": False,
        "stage": "FINAL_FULL_SL_VELOCITY_CIRCUIT",
        "version": VERSION,
        **block,
    }
    state["entry_guard"] = payload
    state["entry_permission"] = payload
    state["entry_attempt"] = payload
    state["last_entry_attempt"] = payload
    state["entry_block_reason"] = block["reason"]
    state["last_entry_block_reason"] = block["reason"]


def apply_full_sl_velocity_circuit_patch() -> bool:
    if getattr(runtime, "_okai_full_sl_velocity_circuit_v1", False):
        return True

    previous_open = runtime._open_common
    previous_close = runtime._close

    def open_with_loss_circuit(
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
        block = _active_block(conn, user_id, _mode(settings))
        if block:
            _set_block(state, block)
            return False
        return previous_open(
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

    def close_with_loss_circuit(
        conn,
        user_id,
        trade,
        price,
        reason,
        order_id=None,
    ):
        result = previous_close(
            conn, user_id, trade, price, reason, order_id
        )
        _register_full_sl(conn, user_id, trade, reason)
        return result

    runtime._open_common = open_with_loss_circuit
    runtime._close = close_with_loss_circuit
    runtime._okai_full_sl_velocity_circuit_v1 = True
    runtime._okai_full_sl_velocity_circuit_version = VERSION
    return True


__all__ = [
    "DAY_STOP_REASON",
    "PAUSE_REASON",
    "VERSION",
    "_active_block",
    "_register_full_sl",
    "apply_full_sl_velocity_circuit_patch",
]
