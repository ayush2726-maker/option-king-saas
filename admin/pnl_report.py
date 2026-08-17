"""Read-only, admin-facing P&L aggregation.

The bot stores SaaS paper and broker-live positions in ``paper_trades`` and
marks them with ``trading_mode``.  The optional static-IP gateway stores its
orders in ``trades``.  This module combines both ledgers without changing any
trade rows, and keeps paper/live plus realised/open values separate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST_OFFSET = timedelta(hours=5, minutes=30)
OPEN_STATUSES = {"OPEN", "PENDING", "EXIT_PENDING"}
IGNORED_STATUSES = {"FAILED", "CANCELLED"}


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


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _timestamp(row):
    for key in ("entry_time", "created_at", "timestamp", "time", "date"):
        value = _value(row, key)
        if value:
            return value
    return None


def _ist_date(value):
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
        return (parsed.astimezone(timezone.utc) + IST_OFFSET).date().isoformat()
    except Exception:
        return None


def _empty_period():
    return {
        "realized_pnl": 0.0,
        "open_pnl": 0.0,
        "net_pnl": 0.0,
        "closed_trades": 0,
        "open_trades": 0,
        "unpriced_open_trades": 0,
        "trade_count": 0,
    }


def _empty_mode():
    return {"today": _empty_period(), "all_time": _empty_period()}


def _empty_user(user):
    return {
        "user_id": int(user["id"]),
        "name": user["name"],
        "email": user["email"],
        "subscription_status": user["subscription_status"],
        "is_active": bool(user["is_active"]),
        "paper": _empty_mode(),
        "live": _empty_mode(),
        "combined": _empty_mode(),
    }


def _round_period(period):
    period["realized_pnl"] = round(period["realized_pnl"], 2)
    period["open_pnl"] = round(period["open_pnl"], 2)
    period["net_pnl"] = round(
        period["realized_pnl"] + period["open_pnl"],
        2,
    )


def _add_trade(period, *, status, pnl, priced=True):
    period["trade_count"] += 1
    if status in OPEN_STATUSES:
        period["open_trades"] += 1
        if priced:
            period["open_pnl"] += pnl
        else:
            period["unpriced_open_trades"] += 1
    else:
        period["closed_trades"] += 1
        period["realized_pnl"] += pnl


def _paper_trade_pnl(row, status, cost_calculator):
    if status in OPEN_STATUSES:
        entry = _number(_value(row, "entry_price"), 0)
        current_raw = _value(row, "last_ltp")
        qty = int(_number(_value(row, "qty"), 0))
        if current_raw is None or entry <= 0 or qty <= 0:
            return 0.0, False
        try:
            costs = cost_calculator(row, exit_price=_number(current_raw))
            return _number(costs.get("net_pnl"), 0), True
        except Exception:
            return (_number(current_raw) - entry) * qty, True

    net_pnl = _value(row, "net_pnl")
    if net_pnl is not None:
        return _number(net_pnl), True

    entry = _number(_value(row, "entry_price"), 0)
    exit_raw = _value(row, "exit_price")
    qty = int(_number(_value(row, "qty"), 0))
    if exit_raw is not None and entry > 0 and qty > 0:
        try:
            costs = cost_calculator(row, exit_price=_number(exit_raw))
            return _number(costs.get("net_pnl"), 0), True
        except Exception:
            pass
    return _number(_value(row, "pnl"), 0), True


def _table_exists(conn, name):
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    )


def _table_columns(conn, name):
    if not _table_exists(conn, name):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


def _select_rows(conn, table):
    if not _table_exists(conn, table):
        return []
    return conn.execute(f"SELECT * FROM {table} ORDER BY id ASC").fetchall()


def build_all_user_pnl_report(conn, now=None, cost_calculator=None):
    """Return per-user and portfolio P&L without mutating the database."""
    if cost_calculator is None:
        from bot.net_pnl_history_patch import calculate_row_net_costs

        cost_calculator = calculate_row_net_costs
    now = now or datetime.now(timezone.utc)
    today_ist = (now.astimezone(timezone.utc) + IST_OFFSET).date().isoformat()

    user_columns = _table_columns(conn, "users")
    status_expr = (
        "subscription_status"
        if "subscription_status" in user_columns
        else "'unknown' AS subscription_status"
    )
    active_expr = "is_active" if "is_active" in user_columns else "1 AS is_active"
    users = conn.execute(
        f"""
        SELECT id, name, email, {status_expr}, {active_expr}
        FROM users
        ORDER BY id ASC
        """
    ).fetchall()
    by_id = {int(row["id"]): _empty_user(row) for row in users}

    for row in _select_rows(conn, "paper_trades"):
        user = by_id.get(int(_number(_value(row, "user_id"), 0)))
        if not user:
            continue
        status = str(_value(row, "status", "CLOSED") or "CLOSED").upper()
        if status in IGNORED_STATUSES:
            continue
        mode = (
            "live"
            if str(_value(row, "trading_mode", "paper") or "paper").lower() == "live"
            else "paper"
        )
        pnl, priced = _paper_trade_pnl(row, status, cost_calculator)
        _add_trade(user[mode]["all_time"], status=status, pnl=pnl, priced=priced)
        if _ist_date(_timestamp(row)) == today_ist:
            _add_trade(user[mode]["today"], status=status, pnl=pnl, priced=priced)

    # Static-IP gateway trades are real broker trades and are not duplicated in
    # paper_trades. Closed P&L is broker-reported; open rows have no live quote
    # column, so they are counted but deliberately excluded from net P&L.
    for row in _select_rows(conn, "trades"):
        user = by_id.get(int(_number(_value(row, "user_id"), 0)))
        if not user:
            continue
        status = str(_value(row, "status", "CLOSED") or "CLOSED").upper()
        if status in IGNORED_STATUSES:
            continue
        is_open = status in OPEN_STATUSES
        pnl = _number(_value(row, "net_pnl", _value(row, "pnl", 0)), 0)
        priced = not is_open
        _add_trade(user["live"]["all_time"], status=status, pnl=pnl, priced=priced)
        if _ist_date(_timestamp(row)) == today_ist:
            _add_trade(user["live"]["today"], status=status, pnl=pnl, priced=priced)

    totals = {"paper": _empty_mode(), "live": _empty_mode(), "combined": _empty_mode()}
    for user in by_id.values():
        for period_name in ("today", "all_time"):
            for mode in ("paper", "live"):
                source = user[mode][period_name]
                target = user["combined"][period_name]
                portfolio = totals[mode][period_name]
                for key in (
                    "realized_pnl",
                    "open_pnl",
                    "closed_trades",
                    "open_trades",
                    "unpriced_open_trades",
                    "trade_count",
                ):
                    target[key] += source[key]
                    portfolio[key] += source[key]
            _round_period(user["paper"][period_name])
            _round_period(user["live"][period_name])
            _round_period(user["combined"][period_name])

        # Flat fields make the response easy for the current mobile app to use.
        user["today_net_pnl"] = user["combined"]["today"]["net_pnl"]
        user["all_time_net_pnl"] = user["combined"]["all_time"]["net_pnl"]

    for period_name in ("today", "all_time"):
        for key in (
            "realized_pnl",
            "open_pnl",
            "closed_trades",
            "open_trades",
            "unpriced_open_trades",
            "trade_count",
        ):
            totals["combined"][period_name][key] = (
                totals["paper"][period_name][key]
                + totals["live"][period_name][key]
            )
        for mode in ("paper", "live", "combined"):
            _round_period(totals[mode][period_name])

    return {
        "success": True,
        "date_ist": today_ist,
        "as_of": now.astimezone(timezone.utc).isoformat(),
        "currency": "INR",
        "pnl_basis": "NET_AFTER_EXECUTION_COSTS_WHEN_AVAILABLE",
        "user_count": len(by_id),
        "totals": totals,
        "users": list(by_id.values()),
        "notes": {
            "unpriced_open_trades": "Excluded from open/net P&L until a live quote is available.",
            "gateway_live": "Broker-reported realised P&L; open gateway positions are unpriced.",
        },
    }
