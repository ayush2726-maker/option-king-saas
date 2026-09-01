"""Serve LIVE dashboard/history directly from Angel local-gateway broker truth.

Paper mode continues through the existing Upstox/paper_trades routes unchanged.
LIVE mode bypasses paper_trades for /bot/trade-live and /bot/trade-history so
Angel fills, quantity, held-contract LTP and execution costs cannot be overwritten
by paper/Upstox state.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.responses import JSONResponse

from auth.routes import get_current_user
from database import get_db
from bot.live_net_pnl_breakeven_patch import calculate_execution_costs

VERSION = "LIVE_BROKER_TRUTH_SPLIT_V1_20260901"
IST = timezone(timedelta(hours=5, minutes=30))


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


def _settings_mode(user_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT settings_json FROM strategy_settings WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        if not row:
            return "paper"
        try:
            data = json.loads(row["settings_json"] or "{}")
        except Exception:
            data = {}
        return "live" if str(data.get("trading_mode") or "paper").lower() == "live" else "paper"
    finally:
        conn.close()


def _gateway_position(row):
    try:
        meta = json.loads(_v(row, "metadata_json", "{}") or "{}")
    except Exception:
        meta = {}
    pos = meta.get("gateway_position") if isinstance(meta, dict) else None
    return pos if isinstance(pos, dict) else {}


def _quote_age(stamp):
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def _costs(row, exit_price):
    qty = max(1, _i(_v(row, "quantity", 0), 1))
    entry = max(0.05, _f(_v(row, "entry_price", 0), 0.05))
    broker = str(_v(row, "broker", "angelone") or "angelone").lower()
    if "angel" in broker:
        broker = "angelone"
    try:
        return dict(calculate_execution_costs(
            broker,
            _underlying(row),
            entry,
            max(0.05, _f(exit_price, entry)),
            qty,
            include_slippage=False,
        ))
    except Exception:
        gross = (max(0.05, _f(exit_price, entry)) - entry) * qty
        return {"market_gross_pnl": gross, "total_charges": 0.0, "net_pnl": gross}


def _trade_view(row, slot=None):
    status_raw = str(_v(row, "status", "") or "").lower()
    status = "OPEN" if status_raw in {"open", "exit_pending", "pending"} else "CLOSED"
    qty = max(0, _i(_v(row, "quantity", 0), 0))
    entry = _f(_v(row, "entry_price", 0), 0)
    exit_price = _f(_v(row, "exit_price", 0), 0)
    pos = _gateway_position(row)
    ltp = _f(pos.get("ltp"), 0)
    current = ltp if status == "OPEN" and ltp > 0 else (exit_price if exit_price > 0 else entry)
    calc = _costs(row, current)
    gross = _f(calc.get("market_gross_pnl"), (current-entry)*qty)
    charges = max(0.0, _f(calc.get("total_charges"), 0))
    net = _f(calc.get("net_pnl"), gross-charges)
    stamp = pos.get("updated_at")
    age = _quote_age(stamp)

    result = {
        "id": _i(_v(row, "id", 0), 0),
        "user_id": _i(_v(row, "user_id", 0), 0),
        "symbol": str(_v(row, "symbol", "") or ""),
        "underlying": _underlying(row),
        "option_type": str(_v(row, "option_type", _v(row, "side", "")) or "").upper(),
        "side": str(_v(row, "option_type", _v(row, "side", "")) or "").upper(),
        "qty": qty,
        "quantity": qty,
        "entry_price": round(entry, 2),
        "exit_price": round(exit_price, 2) if exit_price > 0 else None,
        "entry_time": _v(row, "entry_time") or _v(row, "created_at"),
        "exit_time": _v(row, "exit_time"),
        "created_at": _v(row, "created_at"),
        "status": status,
        "capital_slot": slot,
        "sl_price": _f(_v(row, "sl_price", 0), 0) or None,
        "target_price": _f(_v(row, "target_price", 0), 0) or None,
        "live_price": round(current, 2),
        "current_price": round(current, 2),
        "last_ltp": round(ltp, 2) if ltp > 0 else None,
        "gross_pnl": round(gross, 2),
        "total_charges": round(charges, 2),
        "execution_cost": round(charges, 2),
        "execution_costs": round(charges, 2),
        "cost": round(charges, 2),
        "charges": round(charges, 2),
        "net_pnl": round(net, 2),
        "unrealized_pnl": round(net, 2) if status == "OPEN" else None,
        "pnl": round(net, 2),
        "broker_name": "angelone",
        "broker": "angelone",
        "trading_mode": "live",
        "pnl_basis": "LIVE_ANGEL_ACTUAL_FILLS_MINUS_ESTIMATED_CHARGES",
        "quote_updated_at": stamp,
        "quote_age_seconds": round(age, 1) if age is not None else None,
        "quote_stale": bool(status == "OPEN" and (age is None or age > 15)),
        "quote_source": "ANGEL_LOCAL_GATEWAY_TRADES_TABLE",
        "entry_order_id": _v(row, "broker_order_id"),
        "reason": str(_v(row, "exit_reason", "") or ""),
    }
    return result


def _today(trades):
    today = datetime.now(IST).date()
    chosen = []
    for trade in trades:
        raw = trade.get("entry_time") or trade.get("created_at")
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.astimezone(IST).date() == today:
                chosen.append(trade)
        except Exception:
            continue
    closed = [x for x in chosen if x.get("status") == "CLOSED"]
    opened = [x for x in chosen if x.get("status") == "OPEN"]
    closed_pnl = round(sum(_f(x.get("net_pnl"), 0) for x in closed), 2)
    open_pnl = round(sum(_f(x.get("net_pnl"), 0) for x in opened), 2)
    costs = round(sum(_f(x.get("total_charges"), 0) for x in chosen), 2)
    return {
        "date_ist": today.isoformat(),
        "trades": len(chosen),
        "closed_trades": len(closed),
        "open_trades": len(opened),
        "closed_pnl": closed_pnl,
        "open_pnl": open_pnl,
        "total_pnl": round(closed_pnl + open_pnl, 2),
        "execution_cost": costs,
        "source": "ANGEL_LIVE_BROKER_TRUTH",
    }


def _live_payload(user_id):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM trades WHERE user_id=? AND LOWER(status) IN ('open','exit_pending','pending') ORDER BY id ASC",
            (int(user_id),),
        ).fetchall()
    finally:
        conn.close()
    trades = [_trade_view(row, idx + 1) for idx, row in enumerate(rows)]
    if not trades:
        return {
            "success": True, "open": False, "trade": None, "trades": [],
            "open_positions": [], "open_trade_count": 0,
            "source": "ANGEL_LIVE_BROKER_TRUTH", "display_only": True,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
    first = trades[0]
    return {
        "success": True, "open": True, "trade": first, "trades": trades,
        "open_positions": trades, "open_trade_count": len(trades),
        "live_price": first["live_price"], "gross_pnl": first["gross_pnl"],
        "estimated_exit_costs": first["total_charges"], "net_pnl": first["net_pnl"],
        "runtime_ltp_available": all(x.get("last_ltp") is not None for x in trades),
        "all_quotes_fresh": all(not x.get("quote_stale") for x in trades),
        "stale_trade_ids": [x.get("id") for x in trades if x.get("quote_stale")],
        "source": "ANGEL_LIVE_BROKER_TRUTH", "display_only": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "strategy_entry_calculation_changed": False,
    }


def _history_payload(user_id):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM trades WHERE user_id=? AND LOWER(status) IN ('open','exit_pending','closed') ORDER BY id DESC LIMIT 250",
            (int(user_id),),
        ).fetchall()
    finally:
        conn.close()
    trades = [_trade_view(row) for row in rows]
    today = _today(trades)
    realized = round(sum(_f(x.get("net_pnl"), 0) for x in trades if x.get("status") == "CLOSED"), 2)
    open_pnl = round(sum(_f(x.get("net_pnl"), 0) for x in trades if x.get("status") == "OPEN"), 2)
    return {
        "success": True,
        "paper_trades": trades,
        "trades": trades,
        "count": len(trades),
        "today": today,
        "ledger": {
            "version": VERSION,
            "source": "ANGEL_TRADES_TABLE_ONLY",
            "mode": "live",
            "pnl_basis": "NET_AFTER_EXECUTION_COSTS",
            "realized_pnl": realized,
            "open_pnl": open_pnl,
            "total_pnl": round(realized + open_pnl, 2),
            "total_trades": len(trades),
            "closed_trades": sum(1 for x in trades if x.get("status") == "CLOSED"),
            "open_trades": sum(1 for x in trades if x.get("status") == "OPEN"),
            "today": today,
        },
        "pnl_basis": "LIVE_ANGEL_ACTUAL_FILLS_MINUS_ESTIMATED_CHARGES",
        "history_display": "ANGEL_LIVE_ONLY_V1",
        "execution_cost": round(sum(_f(x.get("total_charges"), 0) for x in trades), 2),
        "mode": "live",
        "broker": "angelone",
    }


class LiveModeBrokerTruthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if path not in {"/bot/trade-live", "/bot/trade-history"}:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        authorization = headers.get("authorization")
        try:
            user = get_current_user(authorization)
        except Exception:
            await self.app(scope, receive, send)
            return

        if _settings_mode(user["id"]) != "live":
            await self.app(scope, receive, send)
            return

        try:
            payload = _live_payload(user["id"]) if path == "/bot/trade-live" else _history_payload(user["id"])
            response = JSONResponse(payload, headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "X-OKAI-Live-Authority": VERSION,
                "X-OKAI-Live-Broker": "angelone",
            })
            await response(scope, receive, send)
        except Exception as exc:
            response = JSONResponse({
                "success": False,
                "message": "Angel live broker truth unavailable: " + str(exc)[:180],
                "version": VERSION,
            }, status_code=500)
            await response(scope, receive, send)


def install(app):
    app.add_middleware(LiveModeBrokerTruthMiddleware)
    print(f"LIVE MODE BROKER TRUTH INSTALLED | {VERSION}")
