"""Repair LIVE Daily Trade History at both response sources used by the app.

The mobile app merges /history/paper first and /bot/trade-history second, so the
bot history row wins on duplicate fields. This patch therefore decorates both
response paths after all live gateway wrappers are installed.

Response/accounting only: no signals, sizing, entries, SL/target or exit logic.
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
        rows = conn.execute(
            "SELECT * FROM trades WHERE user_id=? AND UPPER(symbol)=UPPER(?) ORDER BY id DESC LIMIT 30",
            (user_id, symbol),
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None

    entry = _f(trade.get("entry_price"), 0)
    if entry > 0:
        try:
            return min(rows, key=lambda row: abs(_f(_v(row, "entry_price", entry), entry) - entry))
        except Exception:
            pass
    return rows[0]


def _infer_qty(trade):
    entry = _f(trade.get("entry_price"), 0)
    exit_price = _f(trade.get("exit_price"), 0)
    move = exit_price - entry
    if entry <= 0 or exit_price <= 0 or abs(move) < 1e-9:
        return 0

    for saved in (trade.get("gross_pnl"), trade.get("pnl"), trade.get("net_pnl")):
        pnl = _f(saved, 0)
        if abs(pnl) < 1e-9 or pnl * move <= 0:
            continue
        qty = int(round(pnl / move))
        if qty <= 0 or qty > 100000:
            continue
        if abs(move * qty - pnl) <= max(0.10, abs(pnl) * 0.0001):
            return qty
    return 0


def _persist_repair(conn, data, qty, costs):
    paper_id = _i(data.get("id"), 0)
    if paper_id <= 0 or qty <= 0:
        return
    gross = _f(costs.get("market_gross_pnl"), _f(data.get("pnl"), 0))
    charges = max(0.0, _f(costs.get("total_charges"), 0))
    brokerage = max(0.0, _f(costs.get("brokerage"), 0))
    statutory = max(0.0, _f(costs.get("statutory_charges"), charges - brokerage))
    slippage = max(0.0, _f(costs.get("slippage_cost"), 0))
    net = _f(costs.get("net_pnl"), gross - charges)
    try:
        conn.execute(
            """
            UPDATE paper_trades
            SET qty=?, gross_pnl=?, total_charges=?, brokerage=?,
                statutory_charges=?, slippage_cost=?, net_pnl=?, pnl=?, pnl_basis=?
            WHERE id=?
            """,
            (
                qty, round(gross, 2), round(charges, 2), round(brokerage, 2),
                round(statutory, 2), round(slippage, 2), round(net, 2), round(net, 2),
                str(costs.get("execution_basis") or "LIVE_NET_AFTER_EXECUTION_COSTS"),
                paper_id,
            ),
        )
        conn.commit()
    except Exception:
        pass


def _repair_live_trade(trade):
    if not isinstance(trade, dict):
        return trade

    data = dict(trade)
    status = str(data.get("status") or "").upper()

    if status == "OPEN":
        qty = _i(data.get("qty", data.get("quantity", 0)), 0)
        data["qty"] = qty
        data["quantity"] = qty
        charges = max(0.0, _f(data.get("total_charges", data.get("estimated_exit_costs", 0)), 0))
        for key in ("execution_cost", "execution_costs", "cost", "charges"):
            data[key] = round(charges, 2)
        return data

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

        data["trading_mode"] = "live"
        if broker_row is not None:
            data["broker_name"] = str(_v(broker_row, "broker", "angelone") or "angelone")
        else:
            data["broker_name"] = str(data.get("broker_name") or "angelone")

        costs = {}
        if qty > 0 and _f(data.get("entry_price"), 0) > 0 and _f(data.get("exit_price"), 0) > 0:
            try:
                from bot.net_pnl_history_patch import calculate_row_net_costs
                costs = calculate_row_net_costs(data)
            except Exception:
                costs = {}

        if costs:
            gross = _f(costs.get("market_gross_pnl"), _f(data.get("pnl"), 0))
            charges = max(0.0, _f(costs.get("total_charges"), 0))
            net = _f(costs.get("net_pnl"), gross - charges)
            brokerage = max(0.0, _f(costs.get("brokerage"), 0))
            slippage = max(0.0, _f(costs.get("slippage_cost"), 0))
            statutory = max(0.0, _f(costs.get("statutory_charges"), charges - brokerage))
            data.update({
                "gross_pnl": round(gross, 2),
                "total_charges": round(charges, 2),
                "brokerage": round(brokerage, 2),
                "statutory_charges": round(statutory, 2),
                "slippage_cost": round(slippage, 2),
                "net_pnl": round(net, 2),
                "pnl": round(net, 2),
                "pnl_basis": str(costs.get("execution_basis") or "LIVE_NET_AFTER_EXECUTION_COSTS"),
            })
            _persist_repair(conn, data, qty, costs)

        qty = _i(data.get("qty", data.get("quantity", 0)), 0)
        charges = max(0.0, _f(data.get("total_charges", data.get("execution_cost", 0)), 0))
        data["qty"] = qty
        data["quantity"] = qty
        for key in ("execution_cost", "execution_costs", "cost", "charges"):
            data[key] = round(charges, 2)
        return data
    finally:
        conn.close()


def _wrap_view(module, attr, marker):
    original = getattr(module, attr)
    if getattr(original, marker, False):
        return

    def patched(row):
        return _repair_live_trade(original(row))

    setattr(patched, marker, True)
    setattr(module, attr, patched)


def install_live_daily_history_response_patch():
    global _INSTALLED
    if _INSTALLED:
        return

    import user_panel.routes as user_routes
    import bot.trade_live_routes as trade_routes
    from bot.live_accounting_repair_v3 import install_live_accounting_repair_v3

    _wrap_view(user_routes, "_paper_trade_view", "_okai_live_daily_history_v2")
    _wrap_view(trade_routes, "_trade_view", "_okai_live_daily_history_v2")
    install_live_accounting_repair_v3()
    _INSTALLED = True
