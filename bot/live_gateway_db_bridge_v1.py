"""Direct local-gateway -> paper_trades bridge for LIVE display/accounting.

The local gateway already owns the broker truth (actual fills, quantity and held
contract LTP).  Older response decorators tried to reconstruct that truth later
and could miss when the cloud trade id and paper trade id were unrelated.

This module fixes the boundary itself: every gateway position event immediately
updates the matching paper_trades row.  It also backfills recent LIVE rows at
startup so history created before this release gets quantity and execution costs.
No signal, entry qualification, sizing, SL/target or exit decision is changed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from database import get_db
from bot.live_net_pnl_breakeven_patch import calculate_execution_costs

VERSION = "LIVE_GATEWAY_DB_BRIDGE_V1_20260901"
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
        if key in row.keys() and row[key] is not None:
            return row[key]
    except Exception:
        pass
    if isinstance(row, dict):
        value = row.get(key)
        return default if value is None else value
    return default


def _norm_symbol(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _underlying(symbol, saved=""):
    saved = str(saved or "").upper()
    if saved in {"NIFTY", "BANKNIFTY", "SENSEX"}:
        return saved
    text = _norm_symbol(symbol)
    if "BANKNIFTY" in text:
        return "BANKNIFTY"
    if "SENSEX" in text:
        return "SENSEX"
    return "NIFTY"


def _ensure_paper_columns(conn):
    additions = (
        ("gross_pnl", "REAL"), ("slippage_cost", "REAL"),
        ("total_charges", "REAL"), ("brokerage", "REAL"),
        ("statutory_charges", "REAL"), ("net_pnl", "REAL"),
        ("pnl_basis", "TEXT"), ("charges_json", "TEXT"),
        ("trading_mode", "TEXT"), ("broker_name", "TEXT"),
        ("quote_updated_at", "TEXT"), ("quote_source", "TEXT"),
    )
    for name, kind in additions:
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
        except Exception:
            pass


def _candidate_paper_row(conn, gateway_trade, prefer_open=False):
    user_id = _i(_v(gateway_trade, "user_id"), 0)
    symbol = _norm_symbol(_v(gateway_trade, "symbol", ""))
    entry = _f(_v(gateway_trade, "entry_price", 0), 0)
    if user_id <= 0 or not symbol:
        return None

    try:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE user_id=? ORDER BY id DESC LIMIT 300",
            (user_id,),
        ).fetchall()
    except Exception:
        return None

    matches = [row for row in rows if _norm_symbol(_v(row, "symbol", "")) == symbol]
    if not matches:
        return None

    def score(row):
        status = str(_v(row, "status", "") or "").upper()
        status_penalty = 0 if (not prefer_open or status == "OPEN") else 1_000_000
        row_entry = _f(_v(row, "entry_price", 0), 0)
        entry_penalty = abs(row_entry - entry) if entry > 0 and row_entry > 0 else 10_000
        return (status_penalty + entry_penalty, -_i(_v(row, "id"), 0))

    return min(matches, key=score)


def _gateway_ltp(trade):
    try:
        metadata = json.loads(_v(trade, "metadata_json", "{}") or "{}")
    except Exception:
        metadata = {}
    position = metadata.get("gateway_position") if isinstance(metadata, dict) else None
    if not isinstance(position, dict):
        return 0.0, None
    return _f(position.get("ltp"), 0), position.get("updated_at")


def _apply_closed_costs(conn, paper_id, gateway_trade, qty, entry, exit_price):
    if paper_id <= 0 or qty <= 0 or entry <= 0 or exit_price <= 0:
        return
    symbol = _v(gateway_trade, "symbol", "")
    underlying = _underlying(symbol, _v(gateway_trade, "underlying", ""))
    broker = "angelone"
    try:
        costs = dict(calculate_execution_costs(
            broker, underlying, entry, exit_price, qty, include_slippage=False
        ))
    except Exception:
        return

    gross = _f(costs.get("market_gross_pnl"), (exit_price - entry) * qty)
    charges = max(0.0, _f(costs.get("total_charges"), 0))
    brokerage = max(0.0, _f(costs.get("brokerage"), 0))
    statutory = max(0.0, charges - brokerage)
    net = _f(costs.get("net_pnl"), gross - charges)
    conn.execute(
        """
        UPDATE paper_trades
        SET qty=?, trading_mode='live', broker_name='angelone',
            entry_price=?, exit_price=?, pnl=?, gross_pnl=?, slippage_cost=?,
            total_charges=?, brokerage=?, statutory_charges=?, net_pnl=?,
            pnl_basis=?, charges_json=?
        WHERE id=?
        """,
        (
            qty, entry, exit_price, round(net, 2), round(gross, 2),
            round(_f(costs.get("slippage_cost"), 0), 2), round(charges, 2),
            round(brokerage, 2), round(statutory, 2), round(net, 2),
            "LIVE_GATEWAY_ACTUAL_FILLS_MINUS_ESTIMATED_CHARGES_V1",
            json.dumps(costs, separators=(",", ":"), sort_keys=True), paper_id,
        ),
    )


def _sync_gateway_trade(conn, gateway_trade, event_type="BACKFILL", event=None):
    if gateway_trade is None:
        return False
    event = dict(event or {})
    status = str(_v(gateway_trade, "status", "") or "").lower()
    prefer_open = event_type in {"ENTRY_FILLED", "POSITION_HEARTBEAT"} or status in {"open", "exit_pending"}
    paper = _candidate_paper_row(conn, gateway_trade, prefer_open=prefer_open)
    if paper is None:
        return False

    paper_id = _i(_v(paper, "id"), 0)
    qty = _i(event.get("quantity"), 0) or _i(_v(gateway_trade, "quantity", 0), 0) or _i(_v(paper, "qty", 0), 0)
    entry = _f(event.get("entry_price"), 0) or _f(_v(gateway_trade, "entry_price", 0), 0) or _f(_v(paper, "entry_price", 0), 0)
    ltp_event = _f(event.get("ltp"), 0)
    ltp_saved, ltp_stamp = _gateway_ltp(gateway_trade)
    ltp = ltp_event or ltp_saved
    stamp = str(ltp_stamp or datetime.now(timezone.utc).isoformat())

    if event_type in {"ENTRY_FILLED", "POSITION_HEARTBEAT"} or prefer_open:
        conn.execute(
            """
            UPDATE paper_trades
            SET qty=CASE WHEN ? > 0 THEN ? ELSE qty END,
                entry_price=CASE WHEN ? > 0 THEN ? ELSE entry_price END,
                last_ltp=CASE WHEN ? > 0 THEN ? ELSE last_ltp END,
                quote_updated_at=CASE WHEN ? > 0 THEN ? ELSE quote_updated_at END,
                quote_source=CASE WHEN ? > 0 THEN 'ANGEL_LOCAL_GATEWAY_DB_BRIDGE_V1' ELSE quote_source END,
                trading_mode='live', broker_name='angelone'
            WHERE id=?
            """,
            (qty, qty, entry, entry, ltp, ltp, ltp, stamp, ltp, paper_id),
        )

    exit_price = _f(event.get("exit_price"), 0) or _f(_v(gateway_trade, "exit_price", 0), 0)
    is_closed = event_type == "EXIT_FILLED" or status == "closed"
    if is_closed and exit_price > 0:
        _apply_closed_costs(conn, paper_id, gateway_trade, qty, entry, exit_price)

    return True


def backfill_recent_gateway_truth(user_id=None):
    conn = get_db()
    synced = 0
    try:
        _ensure_paper_columns(conn)
        sql = "SELECT * FROM trades"
        params = []
        if user_id is not None:
            sql += " WHERE user_id=?"
            params.append(int(user_id))
        sql += " ORDER BY id DESC LIMIT 600"
        for trade in conn.execute(sql, tuple(params)).fetchall():
            try:
                if _sync_gateway_trade(conn, trade, "BACKFILL", None):
                    synced += 1
            except Exception:
                continue
        conn.commit()
    finally:
        conn.close()
    return synced


def install_live_gateway_db_bridge():
    global _INSTALLED
    if _INSTALLED:
        return

    import local_gateway.routes as gateway_routes

    original = gateway_routes.record_position_event
    if not getattr(original, "_okai_db_bridge_v1", False):
        def bridged(gateway, event):
            result = original(gateway, event)
            try:
                event_data = dict(event or {})
                trade_id = _i(event_data.get("trade_id"), 0)
                event_type = str(event_data.get("event") or "").upper()
                if trade_id > 0:
                    conn = get_db()
                    try:
                        _ensure_paper_columns(conn)
                        trade = conn.execute(
                            "SELECT * FROM trades WHERE id=? AND user_id=?",
                            (trade_id, int(gateway["user_id"])),
                        ).fetchone()
                        _sync_gateway_trade(conn, trade, event_type, event_data)
                        conn.commit()
                    finally:
                        conn.close()
            except Exception as exc:
                print(f"LIVE GATEWAY DB BRIDGE WARNING | {str(exc)[:180]}")
            return result

        bridged._okai_db_bridge_v1 = True
        gateway_routes.record_position_event = bridged

    try:
        synced = backfill_recent_gateway_truth()
        print(f"LIVE GATEWAY DB BRIDGE INSTALLED | synced={synced} | {VERSION}")
    except Exception as exc:
        print(f"LIVE GATEWAY DB BRIDGE STARTUP WARNING | {str(exc)[:180]}")

    _INSTALLED = True


install_live_gateway_db_bridge()
