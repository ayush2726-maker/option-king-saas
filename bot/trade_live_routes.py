"""Lightweight live price endpoint for the Trade tab.

The full /bot/signal endpoint includes strategy state, recovery and history work.
The Trade tab only needs the open option position's latest runtime LTP, so this
route reads the monitor-updated ``last_ltp`` directly from paper_trades. It is
safe to poll once per second and does not calculate entries or mutate trades.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Header

from auth.routes import get_current_user
from database import get_db


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
    """Return only the current open trade and its latest monitored option LTP."""
    user = get_current_user(authorization)
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
    pnl = round((live_price - entry) * qty, 2)

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
            "unrealized_pnl": pnl,
            "pnl": pnl,
            "capital_slot": _row_value(row, "capital_slot"),
            "trading_mode": str(
                _row_value(row, "trading_mode", "paper") or "paper"
            ),
        },
        "live_price": round(live_price, 2),
        "runtime_ltp_available": bool(has_runtime_ltp),
        "source": "OPEN_TRADE_RUNTIME_LAST_LTP",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "display_only": True,
        "strategy_entry_calculation_changed": False,
    }
