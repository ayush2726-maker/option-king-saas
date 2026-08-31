"""Hotfix gateway position sync to prefer the self-describing event symbol.

Local gateway trade IDs and Railway trade IDs are independent namespaces. If an
unrelated Railway trade happens to have the same integer ID, the older sync path
can bind to the wrong row and ignore the correct event symbol. This patch makes
the V3 gateway symbol/order ID authoritative for app ledger updates.
"""

from datetime import datetime, timezone

from database import get_db
from bot import live_gateway_display_sync_v1 as sync


PATCH_VERSION = "LIVE_GATEWAY_DIRECT_SYMBOL_FIRST_V1"


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
            fields = []
            params = []
            if ltp > 0:
                fields += [
                    "last_ltp=?",
                    "quote_updated_at=?",
                    "quote_source='ANGEL_LOCAL_GATEWAY_DIRECT_SYMBOL'",
                    "quote_failed_at=NULL",
                    "quote_error=NULL",
                    "quote_failure_count=0",
                ]
                params += [ltp, now]
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


def apply_live_gateway_direct_symbol_hotfix():
    sync._mirror_event = _mirror_event_direct_first
