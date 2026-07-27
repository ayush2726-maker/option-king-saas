"""Persist display metrics for every PAPER/LIVE trade.

This is a reporting-only patch. It tracks the highest and lowest option premium
seen after entry, plus clean entry/exit timestamps, so the mobile app can show
how far a trade went in profit/loss even after the trade is closed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bot import auto_portfolio_runtime as runtime


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except (TypeError, ValueError):
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_visibility_columns(conn) -> None:
    for name, kind in [
        ("entry_time", "TEXT"),
        ("exit_time", "TEXT"),
        ("high_price", "REAL"),
        ("low_price", "REAL"),
        ("high_pnl", "REAL"),
        ("low_pnl", "REAL"),
        ("high_net_pnl", "REAL"),
        ("low_net_pnl", "REAL"),
        ("max_favourable_points", "REAL"),
        ("max_adverse_points", "REAL"),
        ("max_favourable_r", "REAL"),
        ("max_adverse_r", "REAL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
        except Exception:
            pass
    conn.commit()


def _entry_time(trade) -> str:
    return str(
        runtime._v(trade, "entry_time", None)
        or runtime._v(trade, "created_at", None)
        or _utc_now()
    )


def _visibility_payload(trade, ltp: float, risk: float | None = None) -> dict:
    entry = _f(runtime._v(trade, "entry_price", 0), 0)
    qty = max(1, _i(runtime._v(trade, "qty", 1), 1))
    current = _f(ltp, entry)

    previous_high = max(
        entry,
        _f(runtime._v(trade, "high_price", 0), 0),
        _f(runtime._v(trade, "peak_price", 0), 0),
    )
    previous_low_raw = _f(runtime._v(trade, "low_price", 0), 0)
    previous_low = previous_low_raw if previous_low_raw > 0 else entry

    high = max(previous_high, current)
    low = max(0.05, min(previous_low, current))

    favourable_points = max(0.0, high - entry)
    adverse_points = max(0.0, entry - low)
    risk_points = max(0.05, _f(risk, _f(runtime._v(trade, "initial_risk", 0), 0.05)))

    high_pnl = favourable_points * qty
    low_pnl = -adverse_points * qty

    return {
        "entry_time": _entry_time(trade),
        "high_price": round(high, 2),
        "low_price": round(low, 2),
        "high_pnl": round(high_pnl, 2),
        "low_pnl": round(low_pnl, 2),
        "high_net_pnl": round(high_pnl, 2),
        "low_net_pnl": round(low_pnl, 2),
        "max_favourable_points": round(favourable_points, 2),
        "max_adverse_points": round(adverse_points, 2),
        "max_favourable_r": round(favourable_points / risk_points, 2),
        "max_adverse_r": round(adverse_points / risk_points, 2),
    }


def apply_trade_visibility_metrics_patch() -> None:
    if getattr(runtime, "_okai_trade_visibility_metrics_v2", False):
        return

    original_ensure_schema = runtime._ensure_schema
    original_update_open = runtime._update_open
    original_close = runtime._close

    def ensure_schema_with_visibility(conn):
        original_ensure_schema(conn)
        _ensure_visibility_columns(conn)

    def update_open_with_visibility(conn, trade, ltp, evaluation):
        original_update_open(conn, trade, ltp, evaluation)
        _ensure_visibility_columns(conn)
        payload = _visibility_payload(trade, ltp, (evaluation or {}).get("risk"))
        try:
            conn.execute(
                """
                UPDATE paper_trades
                SET entry_time=COALESCE(entry_time, ?),
                    high_price=?,
                    low_price=?,
                    high_pnl=?,
                    low_pnl=?,
                    high_net_pnl=?,
                    low_net_pnl=?,
                    max_favourable_points=?,
                    max_adverse_points=?,
                    max_favourable_r=?,
                    max_adverse_r=?
                WHERE id=?
                """,
                (
                    payload["entry_time"],
                    payload["high_price"],
                    payload["low_price"],
                    payload["high_pnl"],
                    payload["low_pnl"],
                    payload["high_net_pnl"],
                    payload["low_net_pnl"],
                    payload["max_favourable_points"],
                    payload["max_adverse_points"],
                    payload["max_favourable_r"],
                    payload["max_adverse_r"],
                    trade["id"],
                ),
            )
            conn.commit()
        except Exception:
            pass

    def close_with_visibility(conn, user_id, trade, price, reason, order_id=None):
        original_close(conn, user_id, trade, price, reason, order_id)
        _ensure_visibility_columns(conn)
        payload = _visibility_payload(trade, price, runtime._v(trade, "initial_risk", None))
        try:
            conn.execute(
                """
                UPDATE paper_trades
                SET entry_time=COALESCE(entry_time, ?),
                    exit_time=COALESCE(exit_time, ?),
                    high_price=MAX(COALESCE(high_price, 0), ?),
                    low_price=CASE
                        WHEN COALESCE(low_price, 0) <= 0 THEN ?
                        ELSE MIN(low_price, ?)
                    END,
                    high_pnl=MAX(COALESCE(high_pnl, 0), ?),
                    low_pnl=MIN(COALESCE(low_pnl, 0), ?),
                    high_net_pnl=MAX(COALESCE(high_net_pnl, 0), ?),
                    low_net_pnl=MIN(COALESCE(low_net_pnl, 0), ?),
                    max_favourable_points=MAX(COALESCE(max_favourable_points, 0), ?),
                    max_adverse_points=MAX(COALESCE(max_adverse_points, 0), ?),
                    max_favourable_r=MAX(COALESCE(max_favourable_r, 0), ?),
                    max_adverse_r=MAX(COALESCE(max_adverse_r, 0), ?)
                WHERE id=?
                """,
                (
                    payload["entry_time"],
                    _utc_now(),
                    payload["high_price"],
                    payload["low_price"],
                    payload["low_price"],
                    payload["high_pnl"],
                    payload["low_pnl"],
                    payload["high_net_pnl"],
                    payload["low_net_pnl"],
                    payload["max_favourable_points"],
                    payload["max_adverse_points"],
                    payload["max_favourable_r"],
                    payload["max_adverse_r"],
                    trade["id"],
                ),
            )
            conn.commit()
        except Exception:
            pass

    runtime._ensure_schema = ensure_schema_with_visibility
    runtime._update_open = update_open_with_visibility
    runtime._close = close_with_visibility
    runtime._okai_trade_visibility_metrics_v1 = True
    runtime._okai_trade_visibility_metrics_v2 = True
