"""Hotfix gateway position sync to prefer the self-describing event symbol.

Local gateway trade IDs and Railway trade IDs are independent namespaces. If an
unrelated Railway trade happens to have the same integer ID, the older sync path
can bind to the wrong row and ignore the correct event symbol. This patch makes
the V3 gateway symbol/order ID authoritative for app ledger updates.

V2 also makes the gateway position metadata authoritative at the /bot/trade-live
response boundary. This prevents a cloud/runtime quote writer from replacing a
fresh local-gateway option LTP with the entry price between 10-second gateway
heartbeats.
"""

import json
from datetime import datetime, timezone

from database import get_db
from bot import live_gateway_display_sync_v1 as sync


PATCH_VERSION = "LIVE_GATEWAY_DIRECT_SYMBOL_FIRST_V2_RESPONSE_AUTHORITY"
_VIEW_PATCHED = False


def _mirror_event_direct_first(gateway, event):
    event = dict(event or {})
    user_id = int(gateway["user_id"])
    trade_id = sync._i(event.get("trade_id"), 0)
    symbol = str(event.get("symbol") or "").strip()
    order_id = str(
        event.get("entry_order_id") or event.get("broker_order_id") or ""
    ).strip()
    kind = str(event.get("event") or "").upper()
    if kind not in {"POSITION_HEARTBEAT", "ENTRY_FILLED", "EXIT_FILLED"}:
        return {"matched": False, "reason": "UNSUPPORTED_EVENT"}

    conn = get_db()
    try:
        sync._ensure_quote_columns(conn)

        # V3+ event identity is authoritative. Never let a coincidentally equal
        # integer trade_id override the symbol supplied by the actual device.
        pt = sync._find_paper_by_symbol(conn, user_id, symbol, order_id)

        # Legacy fallback only when old agents do not send a symbol.
        if not pt and not symbol and trade_id > 0:
            gt = conn.execute(
                "SELECT * FROM trades WHERE id=? AND user_id=? LIMIT 1",
                (trade_id, user_id),
            ).fetchone()
            if gt:
                pt = sync._find_paper(conn, user_id, gt)

        if not pt:
            return {
                "matched": False,
                "reason": "OPEN_APP_TRADE_NOT_FOUND",
                "symbol": symbol,
                "trade_id": trade_id,
            }

        paper_id = sync._i(sync._v(pt, "id", 0), 0)
        now = datetime.now(timezone.utc).isoformat()

        if kind == "POSITION_HEARTBEAT":
            ltp = sync._f(event.get("ltp"), 0.0)
            qty = sync._i(event.get("quantity"), 0)
            entry = sync._f(event.get("entry_price"), 0.0)
            fields = []
            params = []
            if ltp > 0:
                fields += [
                    "last_ltp=?",
                    "quote_updated_at=?",
                    "quote_source='ANGEL_LOCAL_GATEWAY_DIRECT_SYMBOL_V2'",
                    "quote_failed_at=NULL",
                    "quote_error=NULL",
                    "quote_failure_count=0",
                ]
                params += [ltp, now]
            if entry > 0:
                fields.append("entry_price=?")
                params.append(entry)
            if qty > 0:
                fields.append("qty=?")
                params.append(qty)
            if order_id:
                fields.append("entry_order_id=COALESCE(NULLIF(?,''),entry_order_id)")
                params.append(order_id)
            if fields:
                params.append(paper_id)
                conn.execute(
                    f"UPDATE paper_trades SET {', '.join(fields)} "
                    "WHERE id=? AND UPPER(status)='OPEN'",
                    tuple(params),
                )
                conn.commit()
            return {
                "matched": bool(fields),
                "paper_trade_id": paper_id,
                "symbol": symbol,
                "ltp": ltp,
                "qty": qty,
            }

        if kind == "ENTRY_FILLED":
            entry = sync._f(event.get("entry_price"), 0.0)
            qty = sync._i(event.get("quantity"), 0)
            broker_order_id = str(event.get("broker_order_id") or order_id)
            if entry > 0:
                conn.execute(
                    "UPDATE paper_trades SET entry_price=?, last_ltp=?, "
                    "qty=COALESCE(NULLIF(?,0),qty), "
                    "entry_order_id=COALESCE(NULLIF(?,''),entry_order_id), "
                    "quote_updated_at=?, "
                    "quote_source='ANGEL_LOCAL_GATEWAY_ENTRY_FILL' "
                    "WHERE id=?",
                    (entry, entry, qty, broker_order_id, now, paper_id),
                )
                conn.commit()
            return {
                "matched": entry > 0,
                "paper_trade_id": paper_id,
                "symbol": symbol,
                "entry_price": entry,
                "qty": qty,
            }

        # EXIT_FILLED is intentionally left to the gateway service / existing
        # close flow; this hotfix only repairs live display/accounting identity.
        return {"matched": True, "paper_trade_id": paper_id, "symbol": symbol}
    finally:
        conn.close()


def _load_metadata(value):
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _gateway_truth_for_paper(row):
    """Read the exact held-contract heartbeat directly from the gateway ledger."""
    data = dict(row or {})
    if str(data.get("status") or "").upper() != "OPEN":
        return data

    user_id = sync._i(data.get("user_id"), 0)
    symbol = str(data.get("symbol") or "").strip()
    order_id = str(data.get("entry_order_id") or "").strip()
    if user_id <= 0 or (not symbol and not order_id):
        return data

    conn = get_db()
    try:
        gateway = None
        if order_id:
            try:
                gateway = conn.execute(
                    "SELECT * FROM trades WHERE user_id=? AND broker_order_id=? "
                    "AND status IN ('open','exit_pending') ORDER BY id DESC LIMIT 1",
                    (user_id, order_id),
                ).fetchone()
            except Exception:
                gateway = None
        if not gateway and symbol:
            gateway = conn.execute(
                "SELECT * FROM trades WHERE user_id=? AND UPPER(symbol)=UPPER(?) "
                "AND status IN ('open','exit_pending') ORDER BY id DESC LIMIT 1",
                (user_id, symbol),
            ).fetchone()
        if not gateway:
            return data

        metadata = _load_metadata(sync._v(gateway, "metadata_json", "{}"))
        position = metadata.get("gateway_position")
        if not isinstance(position, dict):
            return data

        ltp = sync._f(position.get("ltp"), 0.0)
        if ltp <= 0:
            return data

        gateway_symbol = str(sync._v(gateway, "symbol", "") or "").strip()
        if symbol and gateway_symbol and gateway_symbol.upper() != symbol.upper():
            return data

        quote_time = str(position.get("updated_at") or datetime.now(timezone.utc).isoformat())
        entry = sync._f(sync._v(gateway, "entry_price", 0.0), 0.0)
        qty = sync._i(sync._v(gateway, "quantity", 0), 0)
        gateway_order_id = str(sync._v(gateway, "broker_order_id", "") or "")

        data["last_ltp"] = ltp
        data["quote_updated_at"] = quote_time
        data["quote_source"] = "ANGEL_LOCAL_GATEWAY_METADATA_AUTHORITY_V2"
        data["quote_failed_at"] = None
        data["quote_error"] = None
        data["quote_failure_count"] = 0
        if entry > 0:
            data["entry_price"] = entry
        if qty > 0:
            data["qty"] = qty
        if gateway_order_id:
            data["entry_order_id"] = gateway_order_id

        # Heal the display ledger as well. Even if another runtime wrote a stale
        # value moments earlier, this GET now returns and persists broker truth.
        paper_id = sync._i(data.get("id"), 0)
        if paper_id > 0:
            try:
                sync._ensure_quote_columns(conn)
                conn.execute(
                    "UPDATE paper_trades SET last_ltp=?, quote_updated_at=?, "
                    "quote_source='ANGEL_LOCAL_GATEWAY_METADATA_AUTHORITY_V2', "
                    "quote_failed_at=NULL, quote_error=NULL, quote_failure_count=0 "
                    "WHERE id=? AND UPPER(status)='OPEN'",
                    (ltp, quote_time, paper_id),
                )
                conn.commit()
            except Exception:
                pass
        return data
    finally:
        conn.close()


def _install_trade_live_response_authority():
    global _VIEW_PATCHED
    if _VIEW_PATCHED:
        return

    import bot.trade_live_routes as trade_routes

    previous_view = trade_routes._trade_view
    if getattr(previous_view, "_okai_gateway_response_authority_v2", False):
        _VIEW_PATCHED = True
        return

    def gateway_authoritative_view(row):
        return previous_view(_gateway_truth_for_paper(row))

    gateway_authoritative_view._okai_gateway_response_authority_v2 = True
    trade_routes._trade_view = gateway_authoritative_view
    _VIEW_PATCHED = True


def apply_live_gateway_direct_symbol_hotfix():
    sync._mirror_event = _mirror_event_direct_first
    _install_trade_live_response_authority()
