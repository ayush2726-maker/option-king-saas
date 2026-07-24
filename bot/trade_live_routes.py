"""Lightweight live price endpoint for the Trade tab.

The full /bot/signal endpoint includes strategy state, recovery and history work.
The Trade tab only needs the open option position's latest runtime LTP, so this
route reads the monitor-updated ``last_ltp`` directly from paper_trades. It is
safe to poll once per second and does not calculate entries or mutate trades.

Displayed unrealized P&L is conservative net P&L: estimated round-trip charges
(and Paper slippage) are deducted as though the open trade were exited now.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Header

from auth.routes import get_current_user
from database import get_db
from bot.net_pnl_history_patch import (
    backfill_closed_trade_costs,
    calculate_row_net_costs,
)

# Imported for startup side effects. It installs equity-risk sizing and stronger
# post-loss re-entry protection before the bot/backtest patches are activated.
import bot.risk_control_v2_bootstrap  # noqa: F401


router = APIRouter(prefix="/bot", tags=["Bot"])


def _row_value(row, key, default=None):
    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception:
        pass
    return default


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


@router.get("/trade-live")
def get_live_trade_price(authorization: str = Header(None)):
    """Return the current open trade with latest monitored option LTP and net P&L."""
    user = get_current_user(authorization)

    # Repair old gross-P&L rows before the history/chart UI refreshes. This is
    # idempotent and normally becomes a no-op after the first request.
    try:
        backfill_closed_trade_costs(user["id"])
    except Exception:
        pass

    conn = get_db()

    try:
        row = conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE user_id=? AND status='OPEN'
            ORDER BY COALESCE(capital_slot, 99) ASC, id ASC
            LIMIT 1
            """,
            (user["id"],),
        ).fetchone()
    except Exception as exc:
        conn.close()
        return {
            "success": False,
            "open": False,
            "message": "Live trade price unavailable: " + str(exc)[:120],
            "as_of": datetime.now(timezone.utc).isoformat(),
        }

    conn.close()

    if not row:
        return {
            "success": True,
            "open": False,
            "trade": None,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "display_only": True,
        }

    entry = _number(_row_value(row, "entry_price"), 0.0)
    qty = int(_number(_row_value(row, "qty"), 0))
    raw_ltp = _row_value(row, "last_ltp")
    has_runtime_ltp = raw_ltp is not None
    live_price = _number(raw_ltp, entry) if has_runtime_ltp else entry

    try:
        costs = calculate_row_net_costs(row, exit_price=live_price)
    except Exception:
        costs = {}

    gross_pnl = round(
        _number(costs.get("market_gross_pnl"), (live_price - entry) * qty),
        2,
    )
    total_charges = round(_number(costs.get("total_charges"), 0), 2)
    pnl = round(_number(costs.get("net_pnl"), gross_pnl - total_charges), 2)

    return {
        "success": True,
        "open": True,
        "trade": {
            "id": int(_row_value(row, "id", 0) or 0),
            "symbol": str(_row_value(row, "symbol", "") or ""),
            "underlying": str(_row_value(row, "underlying", "") or ""),
            "side": str(_row_value(row, "side", "") or ""),
            "qty": qty,
            "entry_price": round(entry, 2),
            "live_price": round(live_price, 2),
            "current_price": round(live_price, 2),
            "sl_price": (
                round(_number(_row_value(row, "sl_price")), 2)
                if _row_value(row, "sl_price") is not None
                else None
            ),
            "status": "OPEN",
            "gross_pnl": gross_pnl,
            "estimated_exit_costs": total_charges,
            "total_charges": total_charges,
            "unrealized_pnl": pnl,
            "net_pnl": pnl,
            "pnl": pnl,
            "pnl_basis": str(
                costs.get("execution_basis")
                or "OPEN_NET_AFTER_ESTIMATED_ROUND_TRIP_COSTS"
            ),
            "capital_slot": _row_value(row, "capital_slot"),
            "trading_mode": str(
                _row_value(row, "trading_mode", "paper") or "paper"
            ),
        },
        "live_price": round(live_price, 2),
        "gross_pnl": gross_pnl,
        "estimated_exit_costs": total_charges,
        "net_pnl": pnl,
        "runtime_ltp_available": bool(has_runtime_ltp),
        "source": "OPEN_TRADE_RUNTIME_LAST_LTP",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "display_only": True,
        "strategy_entry_calculation_changed": False,
    }
