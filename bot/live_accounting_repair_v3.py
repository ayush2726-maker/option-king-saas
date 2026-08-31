"""Deterministic repair for legacy LIVE rows with lost qty/mode/cost.

Accounting/display only. Never changes entries, sizing, SL, target or exits.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from bot.live_net_pnl_breakeven_patch import calculate_execution_costs

IST = timezone(timedelta(hours=5, minutes=30))
VERSION = "LIVE_ACCOUNTING_REPAIR_V3"
_INSTALLED = False
LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}


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
    if saved in LOT_SIZES:
        return saved
    symbol = str(_v(row, "symbol", "") or "").upper()
    if "BANKNIFTY" in symbol:
        return "BANKNIFTY"
    if "SENSEX" in symbol:
        return "SENSEX"
    if "NIFTY" in symbol:
        return "NIFTY"
    return ""


def _expected_lot_qty(row):
    underlying = _underlying(row)
    lot = LOT_SIZES.get(underlying, 0)
    if lot <= 0:
        return 0
    entry = _f(_v(row, "entry_price", 0), 0)
    exit_price = _f(_v(row, "exit_price", 0), 0)
    move = exit_price - entry
    if entry <= 0 or exit_price <= 0 or abs(move) < 1e-9:
        return 0

    # Try all legacy P&L fields. A valid repair must reconstruct exactly one
    # exchange lot for the saved broker move (within paise rounding tolerance).
    for key in ("pnl", "gross_pnl", "net_pnl"):
        saved = _v(row, key, None)
        if saved is None:
            continue
        pnl = _f(saved, 0)
        if abs(pnl) < 1e-9 or pnl * move <= 0:
            continue
        expected = move * lot
        if abs(expected - pnl) <= max(0.10, abs(pnl) * 0.0002):
            return lot
    return 0


def _ensure_cost_columns(conn):
    for name, kind in (
        ("gross_pnl", "REAL"), ("slippage_cost", "REAL"),
        ("total_charges", "REAL"), ("brokerage", "REAL"),
        ("statutory_charges", "REAL"), ("net_pnl", "REAL"),
        ("pnl_basis", "TEXT"), ("charges_json", "TEXT"),
        ("trading_mode", "TEXT"), ("broker_name", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
        except Exception:
            pass


def repair_live_user_today(conn, user_id: int) -> int:
    """Repair today's closed rows for a user who is currently in LIVE mode."""
    _ensure_cost_columns(conn)
    today_ist = datetime.now(timezone.utc).astimezone(IST).date()
    day_start = datetime.combine(today_ist, datetime.min.time(), tzinfo=IST).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    start_sql = day_start.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    end_sql = day_end.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        """
        SELECT * FROM paper_trades
        WHERE user_id=? AND UPPER(COALESCE(status,''))='CLOSED'
          AND datetime(created_at)>=datetime(?) AND datetime(created_at)<datetime(?)
        ORDER BY id ASC
        """,
        (int(user_id), start_sql, end_sql),
    ).fetchall()

    repaired = 0
    for row in rows:
        qty = _i(_v(row, "qty", 0), 0)
        if qty <= 0:
            qty = _expected_lot_qty(row)
        if qty <= 0:
            continue

        underlying = _underlying(row) or "NIFTY"
        entry = _f(_v(row, "entry_price", 0), 0)
        exit_price = _f(_v(row, "exit_price", 0), 0)
        if entry <= 0 or exit_price <= 0:
            continue

        broker = str(_v(row, "broker_name", "angelone") or "angelone").lower()
        if broker not in ("angelone", "upstox"):
            broker = "angelone"
        try:
            costs = dict(calculate_execution_costs(
                broker, underlying, entry, exit_price, qty, include_slippage=False
            ))
        except Exception:
            continue

        gross = _f(costs.get("market_gross_pnl"), (exit_price - entry) * qty)
        charges = max(0.0, _f(costs.get("total_charges"), 0))
        brokerage = max(0.0, _f(costs.get("brokerage"), 0))
        slippage = max(0.0, _f(costs.get("slippage_cost"), 0))
        net = _f(costs.get("net_pnl"), gross - charges)
        statutory = max(0.0, charges - brokerage)

        conn.execute(
            """
            UPDATE paper_trades
            SET qty=?, trading_mode='live', broker_name=COALESCE(NULLIF(broker_name,''),?),
                pnl=?, gross_pnl=?, slippage_cost=?, total_charges=?, brokerage=?,
                statutory_charges=?, net_pnl=?, pnl_basis=?, charges_json=?
            WHERE id=?
            """,
            (
                qty, broker, round(net, 2), round(gross, 2), round(slippage, 2),
                round(charges, 2), round(brokerage, 2), round(statutory, 2),
                round(net, 2), "LIVE_NET_AFTER_EXECUTION_COSTS_V3",
                json.dumps(costs, separators=(",", ":"), sort_keys=True), row["id"],
            ),
        )
        repaired += 1

    if repaired:
        total = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(net_pnl,pnl,0)),0) AS pnl, COUNT(*) AS c "
            "FROM paper_trades WHERE user_id=? AND LOWER(COALESCE(trading_mode,''))='live'",
            (int(user_id),),
        ).fetchone()
        try:
            conn.execute(
                "UPDATE bot_status SET total_pnl=?, total_trades=? WHERE user_id=?",
                (round(_f(total["pnl"]), 2), _i(total["c"]), int(user_id)),
            )
        except Exception:
            pass
        conn.commit()
    return repaired


def _wrap_ledger(original):
    if getattr(original, "_okai_live_accounting_v3", False):
        return original
    def wrapped(conn, user_id, settings=None, now=None):
        mode = str((settings or {}).get("trading_mode", "paper") or "paper").lower()
        if mode == "live":
            try:
                repair_live_user_today(conn, int(user_id))
            except Exception:
                pass
        return original(conn, user_id, settings, now)
    wrapped._okai_live_accounting_v3 = True
    return wrapped


def install_live_accounting_repair_v3():
    global _INSTALLED
    if _INSTALLED:
        return
    import bot.routes as bot_routes
    import bot.trade_live_routes as trade_routes

    bot_routes.build_authoritative_ledger = _wrap_ledger(bot_routes.build_authoritative_ledger)
    trade_routes.build_authoritative_ledger = _wrap_ledger(trade_routes.build_authoritative_ledger)
    _INSTALLED = True
