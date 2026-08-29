"""Dependency-free formatting for the Telegram end-of-session report."""

from datetime import date
from html import escape


def _value(row, key, default=None):
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


def _mode(row, fallback):
    return str(_value(row, "trading_mode", fallback) or fallback).strip().lower()


def _status(row):
    return str(_value(row, "status", "") or "").strip().lower()


def _underlying(row):
    saved = str(_value(row, "underlying", "") or "").upper().strip()
    if saved:
        return saved
    symbol = str(_value(row, "symbol", "") or "").upper()
    for name in ("BANKNIFTY", "SENSEX", "NIFTY"):
        if name in symbol:
            return name
    return "OTHER"


def _closed_pnl(row):
    if _status(row) != "closed":
        return 0.0
    net = _value(row, "net_pnl")
    return _number(net if net is not None else _value(row, "pnl"), 0.0)


def _summarize(rows, fallback_mode):
    selected = [row for row in rows if _mode(row, fallback_mode) == fallback_mode]
    closed = [row for row in selected if _status(row) == "closed"]
    open_count = sum(
        1
        for row in selected
        if _status(row) in {"open", "pending", "exit_pending"}
    )
    pnls = [_closed_pnl(row) for row in closed]
    return {
        "trades": len(selected),
        "closed": len(closed),
        "open": open_count,
        "wins": sum(1 for pnl in pnls if pnl > 0),
        "losses": sum(1 for pnl in pnls if pnl < 0),
        "pnl": round(sum(pnls), 2),
    }


def _money(value):
    number = _number(value, 0.0)
    sign = "+" if number > 0 else ""
    return f"₹{sign}{number:.2f}"


def build_daily_trade_report(paper_rows, live_rows, report_date: date) -> str:
    paper = _summarize(list(paper_rows or []), "paper")
    live = _summarize(list(live_rows or []), "live")
    all_rows = [
        *[row for row in (paper_rows or []) if _mode(row, "paper") == "paper"],
        *[row for row in (live_rows or []) if _mode(row, "live") == "live"],
    ]

    index_pnl = {}
    for row in all_rows:
        if _status(row) != "closed":
            continue
        name = _underlying(row)
        index_pnl[name] = round(index_pnl.get(name, 0.0) + _closed_pnl(row), 2)

    lines = [
        "📊 <b>Daily Trade Report</b>",
        f"Date: {escape(report_date.strftime('%d %b %Y'))}",
        "Market Session: CLOSED",
        "",
        "📝 <b>PAPER</b>",
        f"Trades: {paper['trades']} | Closed: {paper['closed']} | Open: {paper['open']}",
        f"Wins/Losses: {paper['wins']}/{paper['losses']}",
        f"Net P&amp;L: <b>{_money(paper['pnl'])}</b>",
        "",
        "🔴 <b>LIVE</b>",
        f"Trades: {live['trades']} | Closed: {live['closed']} | Open: {live['open']}",
        f"Wins/Losses: {live['wins']}/{live['losses']}",
        f"Recorded P&amp;L: <b>{_money(live['pnl'])}</b>",
    ]

    if index_pnl:
        lines.extend(["", "<b>Index-wise closed P&amp;L</b>"])
        for name in ("NIFTY", "SENSEX", "BANKNIFTY", "OTHER"):
            if name in index_pnl:
                lines.append(f"{escape(name)}: {_money(index_pnl[name])}")

    total_trades = paper["trades"] + live["trades"]
    total_pnl = round(paper["pnl"] + live["pnl"], 2)
    lines.extend(["", f"Total closed P&amp;L: <b>{_money(total_pnl)}</b>"])
    if total_trades == 0:
        lines.extend(["", "आज कोई trade execute नहीं हुआ।"])
    return "\n".join(lines)


__all__ = ["build_daily_trade_report"]
