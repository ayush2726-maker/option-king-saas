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
from bot.authoritative_ledger import build_authoritative_ledger

# Imported for startup side effects. It installs equity-risk sizing and stronger
# post-loss re-entry protection before the bot/backtest patches are activated.
import bot.risk_control_v2_bootstrap  # noqa: F401


router = APIRouter(prefix="/bot", tags=["Bot"])


LIVE_QUOTE_STALE_SECONDS = 15


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


def _optional_number(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return number if number == number else None
    except Exception:
        return None


def _round_price(value):
    number = _optional_number(value)
    return round(number, 2) if number is not None else None


def _money(value):
    number = _optional_number(value)
    return "--" if number is None else f"₹{number:.2f}"


def _time_ist(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            parsed = datetime.fromisoformat(text[:-1] + "+00:00")
        else:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        ist = parsed.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
        return ist.strftime("%H:%M IST")
    except Exception:
        return None


def _quote_freshness(row, status):
    """Expose the monitor quote age instead of silently presenting stale LTP."""
    updated_at = _row_value(row, "quote_updated_at")
    age_seconds = None

    if updated_at:
        try:
            text = str(updated_at).strip().replace(" ", "T")
            if text.endswith("Z"):
                parsed = datetime.fromisoformat(text[:-1] + "+00:00")
            else:
                parsed = datetime.fromisoformat(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            age_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds(),
            )
        except Exception:
            age_seconds = None

    is_open = str(status or "").upper() == "OPEN"
    stale = bool(
        is_open
        and (
            age_seconds is None
            or age_seconds > LIVE_QUOTE_STALE_SECONDS
        )
    )
    return {
        "quote_updated_at": updated_at,
        "quote_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "quote_stale": stale,
        "quote_stale_after_seconds": LIVE_QUOTE_STALE_SECONDS,
        "quote_source": _row_value(row, "quote_source"),
        "quote_failed_at": _row_value(row, "quote_failed_at"),
        "quote_error": _row_value(row, "quote_error"),
        "quote_failure_count": int(
            _number(_row_value(row, "quote_failure_count"), 0)
        ),
    }


def _history_metrics(row, status, entry, qty):
    exit_price = _optional_number(_row_value(row, "exit_price"))
    last_ltp = _optional_number(_row_value(row, "last_ltp"))
    peak = _optional_number(_row_value(row, "peak_price"))
    saved_high = _optional_number(_row_value(row, "high_price"))
    saved_low = _optional_number(_row_value(row, "low_price"))

    price_candidates = [entry]
    for value in (exit_price, last_ltp, peak, saved_high, saved_low):
        if value is not None and value > 0:
            price_candidates.append(value)

    current = last_ltp if last_ltp is not None else (exit_price if exit_price is not None else entry)
    high = saved_high if saved_high is not None and saved_high > 0 else max(price_candidates)
    low = saved_low if saved_low is not None and saved_low > 0 else min(price_candidates)

    high_pnl = _optional_number(_row_value(row, "high_net_pnl"))
    if high_pnl is None:
        high_pnl = _optional_number(_row_value(row, "high_pnl"))
    if high_pnl is None:
        high_pnl = max(0.0, (high - entry) * qty)

    low_pnl = _optional_number(_row_value(row, "low_net_pnl"))
    if low_pnl is None:
        low_pnl = _optional_number(_row_value(row, "low_pnl"))
    if low_pnl is None:
        low_pnl = min(0.0, (low - entry) * qty)

    return {
        "current_price": _round_price(current),
        "live_price": _round_price(current),
        "last_ltp": _round_price(last_ltp),
        "high_price": _round_price(high),
        "low_price": _round_price(low),
        "high_pnl": round(high_pnl, 2),
        "high_net_pnl": round(high_pnl, 2),
        "low_pnl": round(low_pnl, 2),
        "low_net_pnl": round(low_pnl, 2),
        "max_favourable_points": round(max(0.0, high - entry), 2),
        "max_adverse_points": round(max(0.0, entry - low), 2),
        "entry_time": _row_value(row, "entry_time") or _row_value(row, "created_at"),
        "exit_time": _row_value(row, "exit_time") or _row_value(row, "closed_at"),
        "current_price_source": "RUNTIME_LAST_LTP" if last_ltp is not None else "EXIT_PRICE",
    }


def _display_reason(row, trade, status):
    base_reason = str(_row_value(row, "reason", "") or "").split("\n", 1)[0].strip()
    if status == "OPEN":
        return base_reason

    high = trade.get("high_price")
    low = trade.get("low_price")
    current = trade.get("current_price")
    high_pnl = _optional_number(trade.get("high_net_pnl") or trade.get("high_pnl"))
    low_pnl = _optional_number(trade.get("low_net_pnl") or trade.get("low_pnl"))
    entry_time = _time_ist(trade.get("entry_time") or _row_value(row, "created_at"))
    exit_time = _time_ist(trade.get("exit_time"))

    parts = [
        f"High {_money(high)}",
        f"Low {_money(low)}",
        f"Now {_money(current)}",
    ]
    if high_pnl is not None:
        parts.append(f"Max +₹{high_pnl:.0f}")
    if low_pnl is not None and low_pnl < 0:
        parts.append(f"Worst ₹{low_pnl:.0f}")

    time_parts = []
    if entry_time:
        time_parts.append(f"Entry {entry_time}")
    if exit_time:
        time_parts.append(f"Exit {exit_time}")

    metric_line = " • ".join(parts)
    time_line = " • ".join(time_parts)
    return "\n".join(x for x in (base_reason, metric_line, time_line) if x)


def _trade_view(row):
    """Return one trade with net P&L fields normalized for the app."""
    trade = dict(row)
    status = str(_row_value(row, "status", "") or "").upper()
    entry = _number(_row_value(row, "entry_price"), 0.0)
    qty = int(_number(_row_value(row, "qty"), 0))

    metrics = _history_metrics(row, status, entry, qty)
    trade.update(metrics)
    trade.update(_quote_freshness(row, status))

    if status == "OPEN":
        current = _number(trade.get("current_price"), entry)
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
        "high_price",
        "low_price",
        "high_pnl",
        "low_pnl",
        "high_net_pnl",
        "low_net_pnl",
        "current_price",
        "live_price",
        "last_ltp",
    ):
        if trade.get(key) is not None:
            trade[key] = round(_number(trade.get(key)), 2)

    trade["reason"] = _display_reason(row, trade, status)
    trade["visibility_metrics"] = {
        "shows_high_low_after_close": True,
        "shows_latest_saved_price_after_close": True,
        "current_price_source": trade.get("current_price_source"),
    }
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
        settings = {
            "trading_mode": "paper",
            "paper_capital": 100000,
        }
        try:
            import json

            settings_row = conn.execute(
                "SELECT settings_json FROM strategy_settings WHERE user_id=?",
                (user["id"],),
            ).fetchone()
            if settings_row:
                settings.update(json.loads(settings_row["settings_json"] or "{}"))
        except Exception:
            pass
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
        ledger = build_authoritative_ledger(conn, user["id"], settings)
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
        "today": ledger["today"],
        "ledger": ledger,
        "pnl_basis": "NET_AFTER_EXECUTION_COSTS",
        "history_display": "HIGH_LOW_NOW_ENTRY_EXIT_TIME_V2",
    }


@router.get("/trade-live")
def get_live_trade_price(authorization: str = Header(None)):
    """Return every open trade with its latest monitored option LTP and net P&L.

    ``trade`` remains the slot-1 compatibility field for older app builds.
    Current builds consume ``trades`` so slot 2/3 never wait for the slower
    history refresh before their price, SL and P&L change on screen.
    """
    user = get_current_user(authorization)

    try:
        backfill_closed_trade_costs(user["id"])
    except Exception:
        pass

    conn = get_db()

    try:
        rows = conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE user_id=? AND status='OPEN'
            ORDER BY COALESCE(capital_slot, 99) ASC, id ASC
            """,
            (user["id"],),
        ).fetchall()
    except Exception as exc:
        conn.close()
        return {
            "success": False,
            "open": False,
            "message": "Live trade price unavailable: " + str(exc)[:120],
            "as_of": datetime.now(timezone.utc).isoformat(),
        }

    conn.close()

    if not rows:
        return {
            "success": True,
            "open": False,
            "trade": None,
            "trades": [],
            "open_positions": [],
            "open_trade_count": 0,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "display_only": True,
        }

    trades = [_trade_view(row) for row in rows]
    trade = trades[0]
    live_price = _number(
        trade.get("live_price"),
        _row_value(rows[0], "entry_price", 0),
    )
    gross_pnl = round(_number(trade.get("gross_pnl"), 0), 2)
    total_charges = round(_number(trade.get("total_charges"), 0), 2)
    pnl = round(_number(trade.get("net_pnl", trade.get("pnl", 0))), 2)

    return {
        "success": True,
        "open": True,
        "trade": trade,
        "trades": trades,
        "open_positions": trades,
        "open_trade_count": len(trades),
        "live_price": round(live_price, 2),
        "gross_pnl": gross_pnl,
        "estimated_exit_costs": total_charges,
        "net_pnl": pnl,
        "runtime_ltp_available": all(
            _row_value(row, "last_ltp") is not None for row in rows
        ),
        "all_quotes_fresh": all(not item.get("quote_stale") for item in trades),
        "stale_trade_ids": [
            item.get("id") for item in trades if item.get("quote_stale")
        ],
        "source": "OPEN_TRADE_RUNTIME_LAST_LTP",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "display_only": True,
        "strategy_entry_calculation_changed": False,
    }
