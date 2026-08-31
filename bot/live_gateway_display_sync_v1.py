"""Synchronize local Angel gateway live positions into the app ledger/display.

Display/accounting bridge only: no signal, sizing, SL/target or exit-rule change.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from database import get_db


VERSION = "LIVE_GATEWAY_DISPLAY_SYNC_V2"
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


def _json(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _ensure_quote_columns(conn):
    try:
        names = {str(r["name"]) for r in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
    except Exception:
        return
    for name, kind in (
        ("quote_updated_at", "TEXT"),
        ("quote_source", "TEXT"),
        ("quote_failed_at", "TEXT"),
        ("quote_error", "TEXT"),
        ("quote_failure_count", "INTEGER DEFAULT 0"),
    ):
        if name not in names:
            try:
                conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
            except Exception:
                pass
    conn.commit()


def _gateway_trade(conn, user_id: int, symbol: str):
    if user_id <= 0 or not symbol:
        return None
    try:
        return conn.execute(
            """
            SELECT * FROM trades
            WHERE user_id=? AND UPPER(symbol)=UPPER(?)
              AND LOWER(status) IN ('open','exit_pending')
            ORDER BY id DESC LIMIT 1
            """,
            (int(user_id), str(symbol)),
        ).fetchone()
    except Exception:
        return None


def _shadow(row):
    """Overlay the broker-confirmed gateway entry/qty/LTP before P&L is calculated."""
    data = dict(row)
    if str(data.get("trading_mode") or "paper").lower() != "live":
        return data

    user_id = _i(data.get("user_id"), 0)
    symbol = str(data.get("symbol") or "")
    conn = get_db()
    try:
        gateway = _gateway_trade(conn, user_id, symbol)
        if not gateway:
            return data

        entry = _f(_v(gateway, "entry_price"), 0.0)
        qty = _i(_v(gateway, "quantity"), 0)
        metadata = _json(_v(gateway, "metadata_json", "{}"))
        position = metadata.get("gateway_position") if isinstance(metadata.get("gateway_position"), dict) else {}
        ltp = _f(position.get("ltp"), 0.0)
        quote_time = str(position.get("updated_at") or "")
        order_id = str(_v(gateway, "broker_order_id", "") or "")

        if entry > 0:
            data["entry_price"] = entry
        if qty > 0:
            data["qty"] = qty
        if order_id:
            data["entry_order_id"] = order_id
        if ltp > 0:
            data["last_ltp"] = ltp
            data["quote_updated_at"] = quote_time or datetime.now(timezone.utc).isoformat()
            data["quote_source"] = "ANGEL_LOCAL_GATEWAY_SHADOW"
            data["quote_failed_at"] = None
            data["quote_error"] = None
            data["quote_failure_count"] = 0

        # Heal the persisted app ledger too, so every screen/history calculation
        # sees the same broker-confirmed values after the first refresh.
        _ensure_quote_columns(conn)
        fields = []
        params = []
        if entry > 0:
            fields.append("entry_price=?")
            params.append(entry)
        if qty > 0:
            fields.append("qty=?")
            params.append(qty)
        if order_id:
            fields.append("entry_order_id=?")
            params.append(order_id)
        if ltp > 0:
            fields += ["last_ltp=?", "quote_updated_at=?", "quote_source='ANGEL_LOCAL_GATEWAY_SHADOW'", "quote_failed_at=NULL", "quote_error=NULL", "quote_failure_count=0"]
            params += [ltp, quote_time or datetime.now(timezone.utc).isoformat()]
        if fields and _i(data.get("id"), 0) > 0:
            params.append(_i(data.get("id"), 0))
            conn.execute(f"UPDATE paper_trades SET {', '.join(fields)} WHERE id=? AND UPPER(status)='OPEN'", tuple(params))
            conn.commit()
        return data
    finally:
        conn.close()


def _find_paper(conn, user_id: int, gateway_trade):
    symbol = str(_v(gateway_trade, "symbol", "") or "")
    order_id = str(_v(gateway_trade, "broker_order_id", "") or "")
    if order_id:
        try:
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE user_id=? AND UPPER(status)='OPEN' AND entry_order_id=? ORDER BY id DESC LIMIT 1",
                (int(user_id), order_id),
            ).fetchone()
            if row:
                return row
        except Exception:
            pass
    try:
        return conn.execute(
            "SELECT * FROM paper_trades WHERE user_id=? AND UPPER(status)='OPEN' AND UPPER(symbol)=UPPER(?) ORDER BY id DESC LIMIT 1",
            (int(user_id), symbol),
        ).fetchone()
    except Exception:
        return None


def _mirror_event(gateway, event):
    event = dict(event or {})
    trade_id = _i(event.get("trade_id"), 0)
    if trade_id <= 0:
        return
    conn = get_db()
    try:
        _ensure_quote_columns(conn)
        gt = conn.execute("SELECT * FROM trades WHERE id=? AND user_id=? LIMIT 1", (trade_id, int(gateway["user_id"]))).fetchone()
        if not gt:
            return
        pt = _find_paper(conn, int(gateway["user_id"]), gt)
        if not pt:
            return
        paper_id = _i(_v(pt, "id"), 0)
        kind = str(event.get("event") or "").upper()
        now = datetime.now(timezone.utc).isoformat()
        if kind == "POSITION_HEARTBEAT":
            ltp = _f(event.get("ltp"), 0.0)
            if ltp > 0:
                conn.execute("UPDATE paper_trades SET last_ltp=?, quote_updated_at=?, quote_source='ANGEL_LOCAL_GATEWAY_POSITION', quote_failed_at=NULL, quote_error=NULL, quote_failure_count=0 WHERE id=? AND UPPER(status)='OPEN'", (ltp, now, paper_id))
                conn.commit()
        elif kind == "ENTRY_FILLED":
            entry = _f(event.get("entry_price"), 0.0)
            qty = _i(event.get("quantity"), 0)
            order_id = str(event.get("broker_order_id") or "")
            if entry > 0:
                conn.execute("UPDATE paper_trades SET entry_price=?, last_ltp=?, qty=COALESCE(NULLIF(?,0),qty), entry_order_id=COALESCE(NULLIF(?,''),entry_order_id), quote_updated_at=?, quote_source='ANGEL_LOCAL_GATEWAY_ENTRY_FILL' WHERE id=?", (entry, entry, qty, order_id, now, paper_id))
                conn.commit()
    finally:
        conn.close()


def _decorate(trade: dict) -> dict:
    out = dict(trade or {})
    qty = _i(out.get("qty"), 0)
    charges = _f(out.get("total_charges"), _f(out.get("estimated_exit_costs"), 0.0))
    out["quantity"] = qty
    out["execution_cost"] = round(charges, 2)
    out["execution_costs"] = round(charges, 2)
    out["cost"] = round(charges, 2)
    out["charges"] = round(charges, 2)
    entry = _f(out.get("entry_price"), 0.0)
    if entry > 0:
        out["average_price"] = round(entry, 2)
        out["avg_price"] = round(entry, 2)
        out["broker_day_average"] = round(entry, 2)
        out["average_price_source"] = "BROKER_CONFIRMED_ENTRY"
    return out


def install_live_gateway_display_sync_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    import local_gateway.routes as gateway_routes
    import bot.trade_live_routes as trade_routes

    original_event = gateway_routes.record_position_event
    if not getattr(original_event, "_okai_gateway_display_sync_v2", False):
        def event_with_sync(gateway, event):
            result = original_event(gateway, event)
            try:
                _mirror_event(gateway, event)
            except Exception:
                pass
            return result
        event_with_sync._okai_gateway_display_sync_v2 = True
        gateway_routes.record_position_event = event_with_sync

    original_view = trade_routes._trade_view
    if not getattr(original_view, "_okai_gateway_display_sync_v2", False):
        def view_with_shadow(row):
            # Important: shadow BEFORE original view so gross/net P&L and charges
            # are computed from real broker entry, real qty and latest gateway LTP.
            return _decorate(original_view(_shadow(row)))
        view_with_shadow._okai_gateway_display_sync_v2 = True
        trade_routes._trade_view = view_with_shadow

    _INSTALLED = True
