"""Synchronize local Angel gateway live positions into the app ledger/display.

This bridge intentionally changes display/accounting synchronization only. It does
not change entry signals, order quantity, SL/target rules or exit decisions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from database import get_db


VERSION = "LIVE_GATEWAY_DISPLAY_SYNC_V1"
_INSTALLED = False


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


def _v(row: Any, key: str, default=None):
    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception:
        pass
    if isinstance(row, dict):
        value = row.get(key)
        return default if value is None else value
    return default


def _ensure_paper_quote_columns(conn) -> None:
    existing = set()
    try:
        existing = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()
        }
    except Exception:
        return

    additions = (
        ("quote_updated_at", "TEXT"),
        ("quote_source", "TEXT"),
        ("quote_failed_at", "TEXT"),
        ("quote_error", "TEXT"),
        ("quote_failure_count", "INTEGER DEFAULT 0"),
    )
    for name, kind in additions:
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
        except Exception:
            pass
    conn.commit()


def _find_live_paper_trade(conn, user_id: int, gateway_trade) -> Any:
    broker_order_id = str(_v(gateway_trade, "broker_order_id", "") or "").strip()
    symbol = str(_v(gateway_trade, "symbol", "") or "").strip()

    if broker_order_id:
        try:
            row = conn.execute(
                """
                SELECT * FROM paper_trades
                WHERE user_id=?
                  AND UPPER(status)='OPEN'
                  AND LOWER(COALESCE(trading_mode, 'paper'))='live'
                  AND entry_order_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (int(user_id), broker_order_id),
            ).fetchone()
            if row:
                return row
        except Exception:
            pass

    if symbol:
        try:
            return conn.execute(
                """
                SELECT * FROM paper_trades
                WHERE user_id=?
                  AND UPPER(status)='OPEN'
                  AND LOWER(COALESCE(trading_mode, 'paper'))='live'
                  AND UPPER(symbol)=UPPER(?)
                ORDER BY id DESC LIMIT 1
                """,
                (int(user_id), symbol),
            ).fetchone()
        except Exception:
            return None
    return None


def _mirror_gateway_event(gateway, event) -> None:
    event = dict(event or {})
    event_type = str(event.get("event") or "").upper()
    trade_id = _i(event.get("trade_id"), 0)
    if trade_id <= 0:
        return

    conn = get_db()
    try:
        _ensure_paper_quote_columns(conn)
        gateway_trade = conn.execute(
            "SELECT * FROM trades WHERE id=? AND user_id=? LIMIT 1",
            (trade_id, int(gateway["user_id"])),
        ).fetchone()
        if not gateway_trade:
            return

        paper_trade = _find_live_paper_trade(
            conn,
            int(gateway["user_id"]),
            gateway_trade,
        )
        if not paper_trade:
            return

        now = datetime.now(timezone.utc).isoformat()
        paper_id = int(_v(paper_trade, "id", 0))

        if event_type == "POSITION_HEARTBEAT":
            ltp = _f(event.get("ltp"), 0.0)
            if ltp <= 0:
                return
            conn.execute(
                """
                UPDATE paper_trades
                SET last_ltp=?,
                    quote_updated_at=?,
                    quote_source='ANGEL_LOCAL_GATEWAY_POSITION',
                    quote_failed_at=NULL,
                    quote_error=NULL,
                    quote_failure_count=0
                WHERE id=? AND UPPER(status)='OPEN'
                """,
                (ltp, now, paper_id),
            )
            conn.commit()
            return

        if event_type == "ENTRY_FILLED":
            entry = _f(event.get("entry_price"), 0.0)
            qty = _i(event.get("quantity"), 0)
            order_id = str(event.get("broker_order_id") or "").strip()
            fields = []
            params = []
            if entry > 0:
                fields += ["entry_price=?", "last_ltp=?"]
                params += [entry, entry]
            if qty > 0:
                fields.append("qty=?")
                params.append(qty)
            if order_id:
                fields.append("entry_order_id=?")
                params.append(order_id)
            fields += [
                "quote_updated_at=?",
                "quote_source='ANGEL_LOCAL_GATEWAY_ENTRY_FILL'",
            ]
            params.append(now)
            if fields:
                params.append(paper_id)
                conn.execute(
                    f"UPDATE paper_trades SET {', '.join(fields)} WHERE id=?",
                    tuple(params),
                )
                conn.commit()
            return

        if event_type == "EXIT_FILLED":
            exit_price = _f(event.get("exit_price"), 0.0)
            if exit_price <= 0:
                return
            qty = max(1, _i(_v(paper_trade, "qty", 1), 1))
            entry = _f(_v(paper_trade, "entry_price", 0.0), 0.0)
            gross = round((exit_price - entry) * qty, 2)
            conn.execute(
                """
                UPDATE paper_trades
                SET exit_price=?,
                    last_ltp=?,
                    status='CLOSED',
                    pnl=?,
                    reason=?,
                    quote_updated_at=?,
                    quote_source='ANGEL_LOCAL_GATEWAY_EXIT_FILL'
                WHERE id=? AND UPPER(status)='OPEN'
                """,
                (
                    exit_price,
                    exit_price,
                    gross,
                    str(event.get("reason") or "LOCAL GATEWAY EXIT FILLED")[:300],
                    now,
                    paper_id,
                ),
            )
            conn.commit()
            try:
                from bot.net_pnl_history_patch import backfill_closed_trade_costs

                backfill_closed_trade_costs(int(gateway["user_id"]))
            except Exception:
                pass
    finally:
        conn.close()


def _weighted_day_entry_average(user_id: int, symbol: str) -> float | None:
    symbol = str(symbol or "").strip()
    if user_id <= 0 or not symbol:
        return None

    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = (start_ist - timedelta(hours=5, minutes=30)).replace(tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(days=1)

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT entry_price, qty
            FROM paper_trades
            WHERE user_id=?
              AND UPPER(symbol)=UPPER(?)
              AND LOWER(COALESCE(trading_mode, 'paper'))='live'
              AND datetime(created_at) >= datetime(?)
              AND datetime(created_at) < datetime(?)
              AND COALESCE(entry_price, 0) > 0
              AND COALESCE(qty, 0) > 0
            """,
            (
                int(user_id),
                symbol,
                start_utc.isoformat(),
                end_utc.isoformat(),
            ),
        ).fetchall()
    except Exception:
        return None
    finally:
        conn.close()

    total_qty = sum(max(0, _i(_v(row, "qty", 0), 0)) for row in rows)
    if total_qty <= 0:
        return None
    weighted = sum(
        _f(_v(row, "entry_price", 0.0), 0.0)
        * max(0, _i(_v(row, "qty", 0), 0))
        for row in rows
    )
    return round(weighted / total_qty, 2)


def _decorate_trade(trade: dict) -> dict:
    out = dict(trade or {})
    qty = _i(out.get("qty"), 0)
    charges = _f(
        out.get("total_charges"),
        _f(out.get("estimated_exit_costs"), 0.0),
    )

    out["quantity"] = qty
    out["execution_cost"] = round(charges, 2)
    out["execution_costs"] = round(charges, 2)
    out["cost"] = round(charges, 2)
    out["charges"] = round(charges, 2)

    entry = _f(out.get("entry_price"), 0.0)
    average = None
    if str(out.get("trading_mode") or "paper").lower() == "live":
        average = _weighted_day_entry_average(
            _i(out.get("user_id"), 0),
            str(out.get("symbol") or ""),
        )
    if average is None or average <= 0:
        average = entry if entry > 0 else None

    if average is not None:
        out["average_price"] = round(average, 2)
        out["avg_price"] = round(average, 2)
        out["broker_day_average"] = round(average, 2)
    out["average_price_source"] = (
        "APP_DAY_WEIGHTED_LIVE_ENTRIES"
        if average is not None
        else "UNAVAILABLE"
    )
    return out


def install_live_gateway_display_sync_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    import local_gateway.routes as gateway_routes
    import bot.trade_live_routes as trade_routes

    original_position_event = gateway_routes.record_position_event
    if not getattr(original_position_event, "_okai_gateway_display_sync_v1", False):
        def record_position_event_with_app_sync(gateway, event):
            result = original_position_event(gateway, event)
            try:
                _mirror_gateway_event(gateway, event)
            except Exception:
                # Never break the gateway acknowledgement for a display sync failure.
                pass
            return result

        record_position_event_with_app_sync._okai_gateway_display_sync_v1 = True
        gateway_routes.record_position_event = record_position_event_with_app_sync

    original_trade_view = trade_routes._trade_view
    if not getattr(original_trade_view, "_okai_gateway_display_sync_v1", False):
        def trade_view_with_display_aliases(row):
            trade = original_trade_view(row)
            return _decorate_trade(trade)

        trade_view_with_display_aliases._okai_gateway_display_sync_v1 = True
        trade_routes._trade_view = trade_view_with_display_aliases

    _INSTALLED = True
