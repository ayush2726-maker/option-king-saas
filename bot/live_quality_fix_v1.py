"""Final LIVE display/accounting/profit-retention repair.

Fixes three user-visible LIVE issues without changing entry qualification:
1) Active trade LTP comes from the exact local-gateway held contract metadata.
2) Closed LIVE quantity/costs are repaired for any valid lot multiple, not only 1 lot.
3) Mature winners retain more of observed peak profit after 1.5R.

V2 makes the repaired broker truth authoritative *after* all older view wrappers
run. Older wrappers were still able to return qty=0/cost=0 or replace the fresh
LTP with entry price, which is exactly what the mobile UI was showing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from database import get_db
from bot.live_net_pnl_breakeven_patch import calculate_execution_costs

VERSION = "LIVE_QUALITY_FIX_V2_RESPONSE_AUTHORITY_20260901"
_INSTALLED = False


def _f(value, default=0.0):
    try:
        x = float(value)
        return x if x == x else float(default)
    except Exception:
        return float(default)


def _i(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _v(row, key, default=None):
    try:
        if key in row.keys() and row[key] is not None:
            return row[key]
    except Exception:
        pass
    if isinstance(row, dict):
        value = row.get(key)
        return default if value is None else value
    return default


def _underlying(row):
    saved = str(_v(row, "underlying", "") or "").upper()
    if saved in {"NIFTY", "BANKNIFTY", "SENSEX"}:
        return saved
    symbol = str(_v(row, "symbol", "") or "").upper()
    if "BANKNIFTY" in symbol:
        return "BANKNIFTY"
    if "SENSEX" in symbol:
        return "SENSEX"
    return "NIFTY"


def _gateway_row(conn, data):
    user_id = _i(data.get("user_id"), 0)
    symbol = str(data.get("symbol") or "").strip()
    order_id = str(data.get("entry_order_id") or "").strip()
    if user_id <= 0:
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
    if symbol:
        try:
            return conn.execute(
                "SELECT * FROM trades WHERE user_id=? AND UPPER(symbol)=UPPER(?) ORDER BY id DESC LIMIT 1",
                (user_id, symbol),
            ).fetchone()
        except Exception:
            pass
    return None


def _infer_qty(data):
    entry = _f(data.get("entry_price"), 0)
    exit_price = _f(data.get("exit_price"), 0)
    move = exit_price - entry
    if entry <= 0 or exit_price <= 0 or abs(move) < 1e-9:
        return 0
    for key in ("gross_pnl", "pnl", "net_pnl"):
        saved = data.get(key)
        if saved is None:
            continue
        pnl = _f(saved, 0)
        if abs(pnl) < 1e-9 or pnl * move <= 0:
            continue
        qty = int(round(pnl / move))
        if qty <= 0 or qty > 100000:
            continue
        if abs(move * qty - pnl) <= max(0.25, abs(pnl) * 0.0005):
            return qty
    return 0


def _ensure_columns(conn):
    for name, kind in (
        ("gross_pnl", "REAL"), ("slippage_cost", "REAL"),
        ("total_charges", "REAL"), ("brokerage", "REAL"),
        ("statutory_charges", "REAL"), ("net_pnl", "REAL"),
        ("pnl_basis", "TEXT"), ("charges_json", "TEXT"),
        ("trading_mode", "TEXT"), ("broker_name", "TEXT"),
        ("quote_updated_at", "TEXT"), ("quote_source", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
        except Exception:
            pass
    conn.commit()


def _repair_row(row):
    data = dict(row or {})
    user_id = _i(data.get("user_id"), 0)
    paper_id = _i(data.get("id"), 0)
    status = str(data.get("status") or "").upper()
    if user_id <= 0 or paper_id <= 0:
        return data

    conn = get_db()
    try:
        _ensure_columns(conn)
        gateway = _gateway_row(conn, data)

        if status == "OPEN":
            if gateway is not None:
                try:
                    metadata = json.loads(_v(gateway, "metadata_json", "{}") or "{}")
                except Exception:
                    metadata = {}
                position = metadata.get("gateway_position") if isinstance(metadata, dict) else None
                if isinstance(position, dict):
                    ltp = _f(position.get("ltp"), 0)
                    if ltp > 0:
                        stamp = str(position.get("updated_at") or datetime.now(timezone.utc).isoformat())
                        data["last_ltp"] = ltp
                        data["quote_updated_at"] = stamp
                        data["quote_source"] = "ANGEL_LOCAL_GATEWAY_EXACT_CONTRACT_V2"
                        qty = _i(_v(gateway, "quantity", 0), 0)
                        entry = _f(_v(gateway, "entry_price", 0), 0)
                        broker = str(_v(gateway, "broker", data.get("broker_name") or "angelone") or "angelone").lower()
                        if "angel" in broker:
                            broker = "angelone"
                        if qty > 0:
                            data["qty"] = qty
                            data["quantity"] = qty
                        if entry > 0:
                            data["entry_price"] = entry
                        data["broker_name"] = broker
                        data["trading_mode"] = "live"
                        conn.execute(
                            "UPDATE paper_trades SET last_ltp=?, quote_updated_at=?, quote_source=?, qty=COALESCE(NULLIF(?,0),qty), entry_price=COALESCE(NULLIF(?,0),entry_price), trading_mode='live', broker_name=COALESCE(NULLIF(broker_name,''),?) WHERE id=? AND UPPER(status)='OPEN'",
                            (ltp, stamp, data["quote_source"], qty, entry, broker, paper_id),
                        )
                        conn.commit()
            return data

        if status != "CLOSED":
            return data

        qty = _i(data.get("qty", data.get("quantity", 0)), 0)
        if qty <= 0 and gateway is not None:
            qty = _i(_v(gateway, "quantity", 0), 0)
        if qty <= 0:
            qty = _infer_qty(data)
        if qty <= 0:
            return data

        entry = _f(data.get("entry_price"), 0)
        exit_price = _f(data.get("exit_price"), 0)
        if entry <= 0 or exit_price <= 0:
            return data

        broker = str(_v(gateway, "broker", data.get("broker_name") or "angelone") if gateway is not None else (data.get("broker_name") or "angelone")).lower()
        if "angel" in broker:
            broker = "angelone"
        if broker not in {"angelone", "upstox", "zerodha"}:
            broker = "angelone"

        costs = dict(calculate_execution_costs(
            broker, _underlying(data), entry, exit_price, qty, include_slippage=False
        ))
        gross = _f(costs.get("market_gross_pnl"), (exit_price-entry)*qty)
        charges = max(0.0, _f(costs.get("total_charges"), 0))
        brokerage = max(0.0, _f(costs.get("brokerage"), 0))
        statutory = max(0.0, charges - brokerage)
        net = _f(costs.get("net_pnl"), gross - charges)

        data.update({
            "qty": qty, "quantity": qty, "trading_mode": "live",
            "broker_name": broker, "gross_pnl": round(gross, 2),
            "total_charges": round(charges, 2), "brokerage": round(brokerage, 2),
            "statutory_charges": round(statutory, 2),
            "slippage_cost": round(_f(costs.get("slippage_cost"), 0), 2),
            "net_pnl": round(net, 2), "pnl": round(net, 2),
            "pnl_basis": "LIVE_ACTUAL_FILLS_MINUS_ESTIMATED_CHARGES_V2",
        })
        for key in ("execution_cost", "execution_costs", "cost", "charges"):
            data[key] = round(charges, 2)

        conn.execute(
            """UPDATE paper_trades SET qty=?, trading_mode='live', broker_name=?, pnl=?, gross_pnl=?, slippage_cost=?, total_charges=?, brokerage=?, statutory_charges=?, net_pnl=?, pnl_basis=?, charges_json=? WHERE id=?""",
            (qty, broker, round(net,2), round(gross,2), round(_f(costs.get("slippage_cost"),0),2), round(charges,2), round(brokerage,2), round(statutory,2), round(net,2), data["pnl_basis"], json.dumps(costs, separators=(",", ":"), sort_keys=True), paper_id),
        )
        conn.commit()
        return data
    finally:
        conn.close()


def _wrap_view(module, attr, marker):
    original = getattr(module, attr)
    if getattr(original, marker, False):
        return

    def patched(row):
        repaired = _repair_row(row)
        result = original(repaired)
        if not isinstance(result, dict):
            return result

        status = str(repaired.get("status") or result.get("status") or "").upper()

        # IMPORTANT: zero-valued fields returned by older wrappers are not
        # authoritative. Prefer the repaired broker truth whenever it is valid.
        repaired_qty = _i(repaired.get("qty", repaired.get("quantity", 0)), 0)
        result_qty = _i(result.get("qty", result.get("quantity", 0)), 0)
        qty = repaired_qty if repaired_qty > 0 else result_qty
        result["qty"] = qty
        result["quantity"] = qty

        repaired_charges = max(0.0, _f(repaired.get("total_charges", repaired.get("execution_cost", 0)), 0))
        result_charges = max(0.0, _f(result.get("total_charges", result.get("execution_cost", 0)), 0))
        charges = repaired_charges if repaired_charges > 0 else result_charges
        if charges > 0:
            result["total_charges"] = round(charges, 2)
        for key in ("execution_cost", "execution_costs", "cost", "charges"):
            result[key] = round(charges, 2)

        if status == "OPEN":
            ltp = _f(repaired.get("last_ltp"), 0)
            entry = _f(repaired.get("entry_price", result.get("entry_price", 0)), 0)
            if ltp > 0:
                # Force the exact held-contract LTP after every older wrapper.
                result["last_ltp"] = round(ltp, 2)
                result["current_price"] = round(ltp, 2)
                result["live_price"] = round(ltp, 2)
                result["quote_updated_at"] = repaired.get("quote_updated_at")
                result["quote_source"] = repaired.get("quote_source") or "ANGEL_LOCAL_GATEWAY_EXACT_CONTRACT_V2"

                if qty > 0 and entry > 0:
                    broker = str(repaired.get("broker_name") or result.get("broker_name") or "angelone").lower()
                    if "angel" in broker:
                        broker = "angelone"
                    try:
                        costs = dict(calculate_execution_costs(
                            broker, _underlying(repaired), entry, ltp, qty, include_slippage=False
                        ))
                    except Exception:
                        costs = {}
                    gross = _f(costs.get("market_gross_pnl"), (ltp-entry)*qty)
                    live_charges = max(0.0, _f(costs.get("total_charges"), 0))
                    net = _f(costs.get("net_pnl"), gross-live_charges)
                    result["gross_pnl"] = round(gross, 2)
                    result["estimated_exit_costs"] = round(live_charges, 2)
                    result["total_charges"] = round(live_charges, 2)
                    result["unrealized_pnl"] = round(net, 2)
                    result["net_pnl"] = round(net, 2)
                    result["pnl"] = round(net, 2)
                    for key in ("execution_cost", "execution_costs", "cost", "charges"):
                        result[key] = round(live_charges, 2)
            return result

        if status == "CLOSED":
            # Closed history must expose the repaired net accounting, even when
            # an older decorator emitted qty=0/cost=0 afterwards.
            for key in (
                "gross_pnl", "slippage_cost", "total_charges", "brokerage",
                "statutory_charges", "net_pnl", "pnl", "pnl_basis",
                "trading_mode", "broker_name",
            ):
                if key in repaired and repaired.get(key) is not None:
                    result[key] = repaired.get(key)
            repaired_charges = max(0.0, _f(repaired.get("total_charges"), 0))
            if repaired_charges > 0:
                for key in ("execution_cost", "execution_costs", "cost", "charges"):
                    result[key] = round(repaired_charges, 2)
                result["total_charges"] = round(repaired_charges, 2)
            result["qty"] = repaired_qty if repaired_qty > 0 else result_qty
            result["quantity"] = result["qty"]

        return result

    setattr(patched, marker, True)
    setattr(module, attr, patched)


def install_live_quality_fix():
    global _INSTALLED
    if _INSTALLED:
        return

    import bot.trade_live_routes as trade_routes
    import user_panel.routes as user_routes
    import bot.authoritative_profit_lock_runtime_patch as profit_lock

    _wrap_view(trade_routes, "_trade_view", "_okai_live_quality_v2")
    _wrap_view(user_routes, "_paper_trade_view", "_okai_live_quality_v2")

    # Keep the existing 4% first-lock trigger, but reduce giveback after a winner
    # is mature. This does not loosen or create new entries.
    profit_lock.PEAK_PROFIT_RETAIN_PERCENT = 80.0
    profit_lock.RUNNER_DISTANCE_R = 0.55
    profit_lock.SMOOTH_TRAIL_DISTANCE_R = 0.45
    profit_lock.TIGHT_TRAIL_DISTANCE_R = 0.35
    profit_lock.AUTHORITY_VERSION = "AUTHORITATIVE_4PCT_PEAK80_PROFIT_RATCHET_V10"

    _INSTALLED = True
    print(f"LIVE QUALITY FIX INSTALLED | {VERSION}")


install_live_quality_fix()
