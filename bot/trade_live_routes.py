"""Lightweight live trade and reliable history endpoints for the mobile app.

The full /bot/signal endpoint includes strategy state, recovery and history work.
These routes read the monitor-updated trade table directly, keep open P&L net of
estimated exit costs, and expose a dedicated history feed that does not depend on
legacy user-panel route wrappers.
"""

from datetime import datetime, timedelta, timezone

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
    if isinstance(row, dict):
        value = row.get(key)
        return default if value is None else value
    return default


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _trade_view(row):
    """Return one trade with net P&L fields normalized for the app."""
    trade = dict(row)
    status = str(_row_value(row, "status", "") or "").upper()
    entry = _number(_row_value(row, "entry_price"), 0.0)
    qty = int(_number(_row_value(row, "qty"), 0))

    if status == "OPEN":
        raw_ltp = _row_value(row, "last_ltp")
        current = _number(raw_ltp, entry) if raw_ltp is not None else entry
        try:
            costs = calculate_row_net_costs(row, exit_price=current)
        except Exception:
            costs = {}

        gross = round(
            _number(costs.get("market_gross_pnl"), (current - entry) * qty),
            2,
        )
        charges = round(_number(costs.get("total_charges"), 0), 2)
        net = round(_number(costs.get("net_pnl"), gross - charges), 2)
        trade.update(
            {
                "current_price": round(current, 2),
                "live_price": round(current, 2),
                "gross_pnl": gross,
                "estimated_exit_costs": charges,
                "total_charges": charges,
                "unrealized_pnl": net,
                "net_pnl": net,
                "pnl": net,
                "pnl_basis": str(
                    costs.get("execution_basis")
                    or "OPEN_NET_AFTER_ESTIMATED_ROUND_TRIP_COSTS"
                ),
            }
        )
    else:
        net_value = _row_value(row, "net_pnl")
        if net_value is not None:
            trade["pnl"] = round(_number(net_value), 2)
            trade["net_pnl"] = round(_number(net_value), 2)
        elif trade.get("pnl") is not None:
            trade["pnl"] = round(_number(trade.get("pnl")), 2)

        for key in (
            "entry_price",
            "exit_price",
            "gross_pnl",
            "slippage_cost",
            "total_charges",
            "brokerage",
            "statutory_charges",
        ):
            if trade.get(key) is not None:
                trade[key] = round(_number(trade.get(key)), 2)

    return trade


def _today_summary(trades):
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    today_key = now_ist.date().isoformat()
    today = []

    for trade in trades:
        raw = (
            trade.get("entry_time")
            or trade.get("created_at")
            or trade.get("timestamp")
            or trade.get("time")
            or trade.get("date")
        )
        if not raw:
            continue
        try:
            text = str(raw).strip().replace(" ", "T")
            if text.endswith("Z"):
                parsed = datetime.fromisoformat(text[:-1] + "+00:00")
            else:
                parsed = datetime.fromisoformat(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            key = (parsed.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()
        except Exception:
            continue
        if key == today_key:
            today.append(trade)

    closed = [
        trade
        for trade in today
        if str(trade.get("status") or "").upper() != "OPEN"
    ]
    opened = [
        trade
        for trade in today
        if str(trade.get("status") or "").upper() == "OPEN"
    ]
    realized = round(sum(_number(trade.get("net_pnl", trade.get("pnl", 0))) for trade in closed), 2)
    open_pnl = round(sum(_number(trade.get("unrealized_pnl", trade.get("pnl", 0))) for trade in opened), 2)
    return {
        "date_ist": today_key,
        "trades": len(today),
        "closed_trades": len(closed),
        "open_trades": len(opened),
        "closed_pnl": realized,
        "open_pnl": open_pnl,
        "total_pnl": round(realized + open_pnl, 2),
    }


@router.get("/trade-history")
def get_trade_history(authorization: str = Header(None)):
    """Return reliable Paper/Live history with net P&L and today's summary."""
    user = get_current_user(authorization)

    try:
        backfill_closed_trade_costs(user["id"])
    except Exception:
        # History must remain readable even if an old row cannot be backfilled.
        pass

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 250
            """,
            (user["id"],),
        ).fetchall()
        trades = [_trade_view(row) for row in rows]
    except Exception as exc:
        return {
            "success": False,
            "paper_trades": [],
            "message": "Trade history unavailable: " + str(exc)[:160],
        }
    finally:
        conn.close()

    return {
        "success": True,
        "paper_trades": trades,
        "count": len(trades),
        "today": _today_summary(trades),
        "pnl_basis": "NET_AFTER_EXECUTION_COSTS",
    }


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

    trade = _trade_view(row)
    live_price = _number(trade.get("live_price"), _row_value(row, "entry_price", 0))
    gross_pnl = round(_number(trade.get("gross_pnl"), 0), 2)
    total_charges = round(_number(trade.get("total_charges"), 0), 2)
    pnl = round(_number(trade.get("net_pnl", trade.get("pnl", 0))), 2)

    return {
        "success": True,
        "open": True,
        "trade": trade,
        "live_price": round(live_price, 2),
        "gross_pnl": gross_pnl,
        "estimated_exit_costs": total_charges,
        "net_pnl": pnl,
        "runtime_ltp_available": _row_value(row, "last_ltp") is not None,
        "source": "OPEN_TRADE_RUNTIME_LAST_LTP",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "display_only": True,
        "strategy_entry_calculation_changed": False,
    }
