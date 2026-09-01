"""Expose authoritative runtime trail state on LIVE Angel trade cards.

LIVE price/P&L/qty remain sourced from the Angel trades table. The server-side
profit-lock engine, however, owns SL/peak/trail telemetry on paper_trades. This
bridge overlays only those risk-management fields on the LIVE display so the
mobile card shows the same logical stop that the runtime exit engine is using.
"""
from __future__ import annotations

import re

from database import get_db
import bot.live_mode_broker_truth_middleware as live_view

VERSION = "LIVE_TRAIL_DISPLAY_BRIDGE_V1_20260901"
_INSTALLED = False


def _norm(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _f(value, default=0.0):
    try:
        x = float(value)
        return x if x == x else float(default)
    except Exception:
        return float(default)


def _runtime_row(user_id, symbol):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE user_id=? AND UPPER(status)='OPEN' ORDER BY id DESC LIMIT 100",
            (int(user_id),),
        ).fetchall()
        wanted = _norm(symbol)
        matches = [r for r in rows if _norm(r["symbol"] if "symbol" in r.keys() else "") == wanted]
        return matches[0] if matches else None
    finally:
        conn.close()


def install():
    global _INSTALLED
    if _INSTALLED:
        return

    original = live_view._trade_view

    def wrapped(row, slot=None):
        result = original(row, slot)
        if str(result.get("status") or "").upper() != "OPEN":
            return result

        try:
            runtime_row = _runtime_row(result.get("user_id"), result.get("symbol"))
            if runtime_row is None:
                return result

            keys = set(runtime_row.keys())
            runtime_sl = _f(runtime_row["sl_price"], 0) if "sl_price" in keys else 0.0
            if runtime_sl > 0:
                result["sl_price"] = round(runtime_sl, 2)
                result["live_sl"] = round(runtime_sl, 2)

            if "trail_stage" in keys:
                result["trail_stage"] = runtime_row["trail_stage"]
            if "peak_price" in keys and runtime_row["peak_price"] is not None:
                result["peak_price"] = round(_f(runtime_row["peak_price"]), 2)
            if "initial_risk" in keys and runtime_row["initial_risk"] is not None:
                result["initial_risk"] = round(_f(runtime_row["initial_risk"]), 2)
            if "trail_updates" in keys:
                result["trail_updates"] = int(_f(runtime_row["trail_updates"], 0))

            result["sl_source"] = "AUTHORITATIVE_RUNTIME_PAPER_TRADE"
            result["trail_display_bridge"] = VERSION
        except Exception:
            pass
        return result

    wrapped._okai_live_trail_display_bridge_v1 = True
    live_view._trade_view = wrapped
    _INSTALLED = True
    print(f"LIVE TRAIL DISPLAY BRIDGE INSTALLED | {VERSION}")


install()
