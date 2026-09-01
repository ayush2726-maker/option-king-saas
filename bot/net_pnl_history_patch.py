"""Make every displayed Paper/Live P&L net of execution costs.

New closes are handled by ``live_net_pnl_breakeven_patch``. This module also
repairs older closed rows that were saved with gross P&L and wraps history
endpoints so the UI receives repaired quantity and execution-cost aliases.
"""

from __future__ import annotations

import json

from auth.routes import get_current_user
from database import get_db
from bot.live_net_pnl_breakeven_patch import calculate_execution_costs


_INSTALLED = False


def _value(row, key, default=None):
    try:
        if key in row.keys() and row[key] is not None:
            return row[key]
    except Exception:
        pass
    if isinstance(row, dict):
        value = row.get(key)
        return default if value is None else value
    return default


def _float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _underlying(row) -> str:
    saved = str(_value(row, "underlying", "") or "").upper()
    if saved in ("NIFTY", "BANKNIFTY", "SENSEX"):
        return saved
    symbol = str(_value(row, "symbol", "") or "").upper()
    if "BANKNIFTY" in symbol:
        return "BANKNIFTY"
    if "SENSEX" in symbol:
        return "SENSEX"
    return "NIFTY"


def _ensure_columns(conn) -> None:
    for name, kind in (
        ("gross_pnl", "REAL"), ("slippage_cost", "REAL"),
        ("total_charges", "REAL"), ("brokerage", "REAL"),
        ("statutory_charges", "REAL"), ("net_pnl", "REAL"),
        ("pnl_basis", "TEXT"), ("charges_json", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
        except Exception:
            pass
    conn.commit()


def _gateway_qty(conn, row) -> int:
    user_id = _int(_value(row, "user_id", 0), 0)
    symbol = str(_value(row, "symbol", "") or "")
    entry_order_id = str(_value(row, "entry_order_id", "") or "")
    if user_id <= 0 or not symbol:
        return 0
    if entry_order_id:
        try:
            match = conn.execute(
                "SELECT quantity FROM trades WHERE user_id=? AND broker_order_id=? ORDER BY id DESC LIMIT 1",
                (user_id, entry_order_id),
            ).fetchone()
            qty = _int(match["quantity"] if match else 0, 0)
            if qty > 0:
                return qty
        except Exception:
            pass
    try:
        rows = conn.execute(
            "SELECT quantity, entry_price FROM trades WHERE user_id=? AND UPPER(symbol)=UPPER(?) "
            "AND COALESCE(quantity,0)>0 ORDER BY id DESC LIMIT 20",
            (user_id, symbol),
        ).fetchall()
        if not rows:
            return 0
        entry = _float(_value(row, "entry_price", 0), 0)
        best = min(rows, key=lambda item: abs(_float(item["entry_price"], entry) - entry))
        return _int(best["quantity"], 0)
    except Exception:
        return 0


def _infer_qty_from_gross(row) -> int:
    entry = _float(_value(row, "entry_price", 0), 0)
    exit_price = _float(_value(row, "exit_price", 0), 0)
    move = exit_price - entry
    if entry <= 0 or exit_price <= 0 or abs(move) < 1e-9:
        return 0
    saved = _value(row, "gross_pnl")
    if saved is None or abs(_float(saved, 0)) < 1e-9:
        saved = _value(row, "pnl")
    pnl = _float(saved, 0)
    if abs(pnl) < 1e-9 or pnl * move <= 0:
        return 0
    candidate = int(round(pnl / move))
    if candidate <= 0 or candidate > 100000:
        return 0
    if abs(move * candidate - pnl) > max(0.05, abs(pnl) * 0.00005):
        return 0
    return candidate


def _repair_missing_qty(conn, row) -> int:
    qty = _int(_value(row, "qty", 0), 0)
    if qty > 0:
        return qty
    qty = _gateway_qty(conn, row)
    if qty <= 0:
        qty = _infer_qty_from_gross(row)
    if qty <= 0:
        return 0
    try:
        conn.execute(
            "UPDATE paper_trades SET qty=? WHERE id=? AND COALESCE(qty,0)<=0",
            (qty, row["id"]),
        )
    except Exception:
        return 0
    return qty


def calculate_row_net_costs(row, exit_price=None) -> dict:
    entry = max(0.05, _float(_value(row, "entry_price", 0.05), 0.05))
    exit_value = max(
        0.05,
        _float(
            exit_price if exit_price is not None else _value(row, "exit_price", entry),
            entry,
        ),
    )
    qty = max(1, _int(_value(row, "qty", 1), 1))
    mode = str(_value(row, "trading_mode", "paper") or "paper").lower()
    broker = str(_value(row, "broker_name", "angelone") or "angelone").lower()
    return dict(
        calculate_execution_costs(
            broker,
            _underlying(row),
            entry,
            exit_value,
            qty,
            include_slippage=(mode != "live"),
        )
    )


def backfill_closed_trade_costs(user_id=None) -> int:
    conn = get_db()
    repaired = 0
    try:
        _ensure_columns(conn)
        try:
            from bot.trade_mode_truth import reconcile_trade_modes
            reconcile_trade_modes(conn, user_id)
        except Exception:
            pass
        sql = """
            SELECT * FROM paper_trades
            WHERE UPPER(COALESCE(status,''))='CLOSED'
              AND entry_price IS NOT NULL
              AND exit_price IS NOT NULL
              AND (
                    COALESCE(qty,0)<=0
                 OR net_pnl IS NULL
                 OR COALESCE(pnl_basis,'')=''
                 OR COALESCE(total_charges,0)<=0
                 OR (LOWER(COALESCE(trading_mode,'paper'))='paper'
                     AND COALESCE(pnl_basis,'') LIKE 'LIVE_%')
                 OR (LOWER(COALESCE(trading_mode,'paper'))='live'
                     AND COALESCE(pnl_basis,'') LIKE 'PAPER_%')
              )
        """
        params = []
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(int(user_id))
        sql += " ORDER BY id ASC LIMIT 5000"

        for row in conn.execute(sql, tuple(params)).fetchall():
            qty = _repair_missing_qty(conn, row)
            if qty <= 0:
                qty = _int(_value(row, "qty", 0), 0)
            if qty <= 0:
                continue

            current = conn.execute(
                "SELECT * FROM paper_trades WHERE id=?",
                (row["id"],),
            ).fetchone() or row
            costs = calculate_row_net_costs(current)
            gross = _float(costs.get("market_gross_pnl"), 0)
            net = _float(costs.get("net_pnl"), 0)
            charges = _float(costs.get("total_charges"), 0)
            brokerage = _float(costs.get("brokerage"), 0)
            statutory = max(0.0, charges - brokerage)

            conn.execute(
                """
                UPDATE paper_trades
                SET pnl=?, gross_pnl=?, slippage_cost=?, total_charges=?,
                    brokerage=?, statutory_charges=?, net_pnl=?, pnl_basis=?, charges_json=?
                WHERE id=?
                """,
                (
                    round(net, 2),
                    round(gross, 2),
                    round(_float(costs.get("slippage_cost"), 0), 2),
                    round(charges, 2),
                    round(brokerage, 2),
                    round(statutory, 2),
                    round(net, 2),
                    str(costs.get("execution_basis") or "NET_AFTER_COSTS"),
                    json.dumps(costs, separators=(",", ":"), sort_keys=True),
                    row["id"],
                ),
            )
            repaired += 1

        if user_id is not None:
            try:
                summary = conn.execute(
                    """
                    SELECT COALESCE(SUM(pnl),0) AS pnl, COUNT(*) AS trades
                    FROM paper_trades
                    WHERE user_id=? AND UPPER(COALESCE(status,''))='CLOSED'
                    """,
                    (int(user_id),),
                ).fetchone()
                conn.execute(
                    "UPDATE bot_status SET total_pnl=?, total_trades=? WHERE user_id=?",
                    (
                        round(_float(summary["pnl"] if summary else 0), 2),
                        _int(summary["trades"] if summary else 0),
                        int(user_id),
                    ),
                )
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()
    return repaired


def _wrap_route(router, path, wrapper_factory) -> None:
    for route in getattr(router, "routes", []):
        if getattr(route, "path", "") != path:
            continue
        original = route.endpoint
        if getattr(original, "_okai_net_pnl_wrapped", False):
            return
        wrapped = wrapper_factory(original)
        wrapped._okai_net_pnl_wrapped = True
        route.endpoint = wrapped
        try:
            route.dependant.call = wrapped
        except Exception:
            pass
        return


def _decorate_history_trade(trade):
    if not isinstance(trade, dict):
        return trade
    qty = _int(trade.get("qty", trade.get("quantity", 0)), 0)
    charges = max(0.0, _float(trade.get("total_charges", 0), 0))
    trade["qty"] = qty
    trade["quantity"] = qty
    trade["execution_cost"] = round(charges, 2)
    trade["execution_costs"] = round(charges, 2)
    trade["cost"] = round(charges, 2)
    trade["charges"] = round(charges, 2)
    if str(trade.get("status") or "").upper() == "CLOSED":
        net = trade.get("net_pnl")
        if net is not None:
            trade["pnl"] = round(_float(net), 2)
    elif trade.get("pnl") is not None:
        trade["pnl"] = round(_float(trade.get("pnl")), 2)
    return trade


def _decorate_result(result):
    if isinstance(result, dict):
        for trade in result.get("paper_trades", []):
            _decorate_history_trade(trade)
        summary = result.get("summary")
        if isinstance(summary, dict):
            charges = sum(
                _float(t.get("total_charges", t.get("execution_cost", 0)), 0)
                for t in result.get("paper_trades", [])
                if isinstance(t, dict)
            )
            summary["execution_cost"] = round(charges, 2)
            summary["execution_costs"] = round(charges, 2)
            summary["total_charges"] = round(charges, 2)
    return result


def install_net_pnl_history_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from user_panel import routes as user_routes
    from paper import routes as paper_routes

    def history_wrapper(original):
        def endpoint(authorization: str = None):
            user = get_current_user(authorization)
            backfill_closed_trade_costs(user["id"])
            return _decorate_result(original(authorization))
        endpoint.__name__ = getattr(original, "__name__", "paper_history")
        endpoint.__annotations__ = getattr(original, "__annotations__", {})
        return endpoint

    def day_wrapper(original):
        def endpoint(date: str, authorization: str = None):
            user = get_current_user(authorization)
            backfill_closed_trade_costs(user["id"])
            return _decorate_result(original(date, authorization))
        endpoint.__name__ = getattr(original, "__name__", "paper_history_day")
        endpoint.__annotations__ = getattr(original, "__annotations__", {})
        return endpoint

    def account_wrapper(original):
        def endpoint(authorization: str = None):
            user = get_current_user(authorization)
            backfill_closed_trade_costs(user["id"])
            result = original(authorization)
            account = result.get("account", {}) if isinstance(result, dict) else {}
            for key in ("total_pnl", "equity"):
                if key in account:
                    account[key] = round(_float(account.get(key)), 2)
            return result
        endpoint.__name__ = getattr(original, "__name__", "paper_account")
        endpoint.__annotations__ = getattr(original, "__annotations__", {})
        return endpoint

    _wrap_route(user_routes.router, "/history/paper", history_wrapper)
    _wrap_route(user_routes.router, "/history/paper/day", day_wrapper)
    _wrap_route(paper_routes.router, "/paper/account", account_wrapper)
    _INSTALLED = True
