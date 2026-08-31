"""Repair LIVE daily-history display directly at the user-panel response boundary.

This is intentionally response/accounting only. It does not change signals,
entries, sizing, SL/target or exit execution.
"""

from __future__ import annotations

from database import get_db

_INSTALLED = False


def _f(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _i(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _v(row, key, default=None):
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


def _broker_trade(conn, trade):
    user_id = _i(trade.get("user_id"), 0)
    symbol = str(trade.get("symbol") or "")
    order_id = str(trade.get("entry_order_id") or "")
    if user_id <= 0 or not symbol:
        return None

    if order_id:
        try:
            row = conn.execute(
                "SELECT * FROM trades WHERE user_id=? AND broker_order_id=? ORDER BY id DESC LIMIT 1",
                (user_id, order_id),
            ).fetchone()
            if row:
                return row
        except Exception:
            pass

    try:
        return conn.execute(
            "SELECT * FROM trades WHERE user_id=? AND UPPER(symbol)=UPPER(?) ORDER BY id DESC LIMIT 1",
            (user_id, symbol),
        ).fetchone()
    except Exception:
        return None


def _infer_qty(trade):
    entry = _f(trade.get("entry_price"), 0)
    exit_price = _f(trade.get("exit_price"), 0)
    move = exit_price - entry
    if entry <= 0 or exit_price <= 0 or abs(move) < 1e-9:
        return 0

    # Old LIVE rows in this app saved gross broker move in pnl/net_pnl while qty
    # was lost. This deterministic recovery is valid only when it lands exactly
    # on an integer quantity.
    saved = trade.get("gross_pnl")
    if saved is None or abs(_f(saved, 0)) < 1e-9:
        saved = trade.get("pnl")
    pnl = _f(saved, 0)
    if abs(pnl) < 1e-9 or pnl * move <= 0:
        return 0

    qty = int(round(pnl / move))
    if qty <= 0 or qty > 100000:
        return 0
    if abs(move * qty - pnl) > max(0.05, abs(pnl) * 0.00005):
        return 0
    return qty


def _repair_live_trade(trade):
    if not isinstance(trade, dict):
        return trade
    if str(trade.get("status") or "").upper() != "CLOSED":
        return trade

    data = dict(trade)
    conn = get_db()
    try:
        broker_row = _broker_trade(conn, data)
        qty = _i(data.get("qty", data.get("quantity", 0)), 0)
        if qty <= 0 and broker_row is not None:
            qty = _i(_v(broker_row, "quantity", 0), 0)
        if qty <= 0:
            qty = _infer_qty(data)

        if qty > 0:
            data["qty"] = qty
            data["quantity"] = qty
            paper_id = _i(data.get("id"), 0)
            if paper_id > 0:
                try:
                    conn.execute(
                        "UPDATE paper_trades SET qty=? WHERE id=? AND COALESCE(qty,0)<=0",
                        (qty, paper_id),
                    )
                    conn.commit()
                except Exception:
                    pass

        # A matching broker-side trade is authoritative proof this row is LIVE.
        if broker_row is not None:
            data["trading_mode"] = "live"
            broker_name = str(_v(broker_row, "broker", "angelone") or "angelone")
            data["broker_name"] = broker_name

        if qty > 0:
            try:
                from bot.net_pnl_history_patch import calculate_row_net_costs

                costs = calculate_row_net_costs(data)
                gross = _f(costs.get("market_gross_pnl"), _f(data.get("pnl"), 0))
                charges = max(0.0, _f(costs.get("total_charges"), 0))
                net = _f(costs.get("net_pnl"), gross - charges)
                brokerage = max(0.0, _f(costs.get("brokerage"), 0))
                slippage = max(0.0, _f(costs.get("slippage_cost"), 0))

                data["gross_pnl"] = round(gross, 2)
                data["total_charges"] = round(charges, 2)
                data["execution_cost"] = round(charges, 2)
                data["execution_costs"] = round(charges, 2)
                data["cost"] = round(charges, 2)
                data["charges"] = round(charges, 2)
                data["brokerage"] = round(brokerage, 2)
                data["slippage_cost"] = round(slippage, 2)
                data["net_pnl"] = round(net, 2)
                data["pnl"] = round(net, 2)
                data["pnl_basis"] = str(
                    costs.get("execution_basis") or "LIVE_NET_AFTER_EXECUTION_COSTS"
                )
            except Exception:
                pass

        # Always provide the exact aliases consumed by the mobile history card.
        qty = _i(data.get("qty", data.get("quantity", 0)), 0)
        charges = max(0.0, _f(data.get("total_charges", data.get("execution_cost", 0)), 0))
        data["qty"] = qty
        data["quantity"] = qty
        data["execution_cost"] = round(charges, 2)
        data["execution_costs"] = round(charges, 2)
        data["cost"] = round(charges, 2)
        data["charges"] = round(charges, 2)
        return data
    finally:
        conn.close()


def install_live_daily_history_response_patch():
    global _INSTALLED
    if _INSTALLED:
        return

    import user_panel.routes as user_routes

    original = user_routes._paper_trade_view
    if getattr(original, "_okai_live_daily_history_v1", False):
        _INSTALLED = True
        return

    def patched(row):
        base = original(row)
        return _repair_live_trade(base)

    patched._okai_live_daily_history_v1 = True
    user_routes._paper_trade_view = patched
    _INSTALLED = True
