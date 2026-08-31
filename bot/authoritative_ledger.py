"""One database-backed P&L ledger for every dashboard surface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


IST = timezone(timedelta(hours=5, minutes=30))
VERSION = "OKAI-AUTHORITATIVE-LEDGER-V2-LIVE-PROOF"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _value(row: Any, key: str, default=None):
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


def _columns(conn, table="paper_trades") -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _parse(value: Any):
    try:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _sql_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _open_net(row: Any) -> float:
    entry = _f(_value(row, "entry_price"), 0.0)
    current = _f(_value(row, "last_ltp", entry), entry)
    qty = max(1, int(_f(_value(row, "qty", 1), 1)))
    try:
        from bot.net_pnl_history_patch import calculate_row_net_costs
        costs = calculate_row_net_costs(row, exit_price=current) or {}
        return round(_f(costs.get("net_pnl"), (current - entry) * qty), 2)
    except Exception:
        return round((current - entry) * qty, 2)


def _mode_clause(conn, mode: str, paper_columns: set[str]):
    """Classify legacy LIVE rows by broker proof, not only trading_mode.

    Older Angel rows were mirrored into paper_trades before trading_mode was
    reliably persisted, so many genuine LIVE rows still say/default to 'paper'.
    A matching row in the broker-side trades table or an entry order id is
    authoritative evidence that the display-ledger row is LIVE.
    """
    has_mode = "trading_mode" in paper_columns
    trade_columns = _columns(conn, "trades")
    has_trades = bool(trade_columns)
    has_broker_order = "broker_order_id" in trade_columns
    has_entry_order = "entry_order_id" in paper_columns

    proof_parts = []
    if has_mode:
        proof_parts.append("LOWER(COALESCE(paper_trades.trading_mode,''))='live'")
    if has_entry_order:
        proof_parts.append("COALESCE(NULLIF(paper_trades.entry_order_id,''),'')<>''")
    if has_trades:
        match = [
            "t.user_id=paper_trades.user_id",
            "UPPER(COALESCE(t.symbol,''))=UPPER(COALESCE(paper_trades.symbol,''))",
        ]
        if has_broker_order and has_entry_order:
            match.append(
                "(COALESCE(NULLIF(paper_trades.entry_order_id,''),'')='' "
                "OR t.broker_order_id=paper_trades.entry_order_id)"
            )
        proof_parts.append("EXISTS (SELECT 1 FROM trades t WHERE " + " AND ".join(match) + ")")

    if not proof_parts:
        if has_mode:
            return "LOWER(COALESCE(paper_trades.trading_mode,'paper'))=?", [mode]
        return "1=1", []

    live_proof = "(" + " OR ".join(proof_parts) + ")"
    return (live_proof, []) if mode == "live" else (f"NOT {live_proof}", [])


def build_authoritative_ledger(
    conn,
    user_id: int,
    settings: dict | None = None,
    now: datetime | None = None,
) -> dict:
    settings = dict(settings or {})
    columns = _columns(conn)
    mode = "live" if str(settings.get("trading_mode", "paper")).lower() == "live" else "paper"

    # Repair historical LIVE qty/cost before dashboard totals are read. This is
    # accounting-only and never changes entries, sizing or exit execution.
    if mode == "live":
        try:
            from bot.net_pnl_history_patch import backfill_closed_trade_costs
            backfill_closed_trade_costs(int(user_id))
        except Exception:
            pass

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today_ist = current.astimezone(IST).date()
    day_start = datetime.combine(today_ist, datetime.min.time(), tzinfo=IST).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)

    reset_at = _parse(settings.get("paper_capital_reset_at")) if mode == "paper" else None
    cycle_start = reset_at
    today_scope_start = max(day_start, reset_at) if reset_at else day_start

    pnl_expr = "COALESCE(net_pnl, pnl, 0)" if "net_pnl" in columns else "COALESCE(pnl, 0)"
    created_expr = "created_at" if "created_at" in columns else "datetime('now')"
    close_parts = [name for name in ("exit_time", "closed_at", "updated_at", "created_at") if name in columns]
    close_expr = "COALESCE(" + ", ".join(f"NULLIF({name}, '')" for name in close_parts) + ")" if len(close_parts) > 1 else (close_parts[0] if close_parts else created_expr)
    mode_sql, mode_params = _mode_clause(conn, mode, columns)

    def closed_sum(start=None, end=None):
        where = ["paper_trades.user_id=?", "UPPER(paper_trades.status)='CLOSED'", mode_sql]
        params: list[Any] = [int(user_id), *mode_params]
        if start:
            where.append(f"datetime({close_expr}) >= datetime(?)")
            params.append(_sql_time(start))
        if end:
            where.append(f"datetime({close_expr}) < datetime(?)")
            params.append(_sql_time(end))
        row = conn.execute(
            f"SELECT COALESCE(SUM({pnl_expr}),0) AS pnl, COUNT(*) AS count FROM paper_trades WHERE {' AND '.join(where)}",
            tuple(params),
        ).fetchone()
        return round(_f(_value(row, "pnl")), 2), int(_f(_value(row, "count"), 0))

    cycle_realized, cycle_closed = closed_sum(cycle_start, None)
    today_realized, today_closed = closed_sum(today_scope_start, day_end)

    open_where = ["paper_trades.user_id=?", "UPPER(paper_trades.status)='OPEN'", mode_sql]
    open_params: list[Any] = [int(user_id), *mode_params]
    open_rows = conn.execute(
        f"SELECT paper_trades.* FROM paper_trades WHERE {' AND '.join(open_where)} ORDER BY id ASC",
        tuple(open_params),
    ).fetchall()
    open_pnl = round(sum(_open_net(row) for row in open_rows), 2)

    trade_where = ["paper_trades.user_id=?", mode_sql]
    trade_params: list[Any] = [int(user_id), *mode_params]
    if cycle_start:
        trade_where.append(f"datetime({created_expr}) >= datetime(?)")
        trade_params.append(_sql_time(cycle_start))
    total_trades = int(conn.execute(
        f"SELECT COUNT(*) AS count FROM paper_trades WHERE {' AND '.join(trade_where)}",
        tuple(trade_params),
    ).fetchone()["count"])

    today_trade_where = [
        "paper_trades.user_id=?", mode_sql,
        f"datetime({created_expr}) >= datetime(?)",
        f"datetime({created_expr}) < datetime(?)",
    ]
    today_trade_params: list[Any] = [
        int(user_id), *mode_params, _sql_time(today_scope_start), _sql_time(day_end)
    ]
    today_trades = int(conn.execute(
        f"SELECT COUNT(*) AS count FROM paper_trades WHERE {' AND '.join(today_trade_where)}",
        tuple(today_trade_params),
    ).fetchone()["count"])

    if mode == "paper":
        starting_capital = max(1000.0, _f(settings.get("paper_capital", 100000), 100000))
        capital_source = "PAPER_SEED_PLUS_AUTHORITATIVE_NET_LEDGER"
    else:
        bases = [_f(_value(row, "capital_base"), 0) for row in open_rows]
        starting_capital = max([value for value in bases if value > 0] or [0.0]) or None
        capital_source = "LIVE_BROKER_BASE_PLUS_AUTHORITATIVE_NET_LEDGER"

    current_capital = None
    if starting_capital is not None:
        current_capital = round(
            starting_capital + (cycle_realized if mode == "paper" else 0) + open_pnl,
            2,
        )

    return {
        "version": VERSION,
        "source": "PAPER_TRADES_DB_NET_PNL_WITH_LIVE_BROKER_PROOF",
        "mode": mode,
        "pnl_basis": "NET_AFTER_EXECUTION_COSTS",
        "reset_at": _sql_time(reset_at) if reset_at else None,
        "starting_capital": round(starting_capital, 2) if starting_capital is not None else None,
        "realized_pnl": cycle_realized,
        "open_pnl": open_pnl,
        "total_pnl": round(cycle_realized + open_pnl, 2),
        "current_capital": current_capital,
        "total_trades": total_trades,
        "closed_trades": cycle_closed,
        "open_trades": len(open_rows),
        "today": {
            "date_ist": today_ist.isoformat(),
            "trades": today_trades,
            "closed_trades": today_closed,
            "open_trades": len(open_rows),
            "closed_pnl": today_realized,
            "open_pnl": open_pnl,
            "total_pnl": round(today_realized + open_pnl, 2),
            "source": "AUTHORITATIVE_LEDGER_LIVE_BROKER_PROOF",
        },
        "capital_source": capital_source,
    }


__all__ = ["VERSION", "build_authoritative_ledger"]
