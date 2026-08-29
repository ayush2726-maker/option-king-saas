"""All-time, user-scoped performance comparison for supported indices."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


INSTRUMENTS = ("NIFTY", "BANKNIFTY", "SENSEX")
IST = timezone(timedelta(hours=5, minutes=30))
VERSION = "OKAI-INDEX-REPORT-CARD-V2-MODE-AWARE"


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


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _instrument(row: Any) -> str:
    saved = str(_value(row, "underlying", "") or "").upper().strip()
    if saved in INSTRUMENTS:
        return saved

    symbol = str(_value(row, "symbol", "") or "").upper()
    if "BANKNIFTY" in symbol or "BANK NIFTY" in symbol:
        return "BANKNIFTY"
    if "SENSEX" in symbol:
        return "SENSEX"
    if "NIFTY" in symbol:
        return "NIFTY"
    return "UNKNOWN"


def _mode(row: Any, default_mode: str = "paper") -> str:
    saved = str(_value(row, "trading_mode", default_mode) or default_mode).lower()
    return "live" if saved == "live" else "paper"


def _closed_pnl(row: Any) -> float:
    net = _value(row, "net_pnl", None)
    return round(
        _number(net if net is not None else _value(row, "pnl", 0), 0),
        2,
    )


def _empty_summary(instrument: str) -> dict:
    return {
        "instrument": instrument,
        "total_trades": 0,
        "closed_trades": 0,
        "open_trades": 0,
        "profit_trades": 0,
        "loss_trades": 0,
        "breakeven_trades": 0,
        "win_rate": 0.0,
        "realized_pnl": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "average_pnl": 0.0,
        "best_trade": None,
        "worst_trade": None,
        "profit_factor": None,
        "no_loss_trade": False,
        "rank": None,
        "is_best": False,
    }


def _finalize(summary: dict) -> dict:
    closed = int(summary["closed_trades"])
    wins = int(summary["profit_trades"])
    gross_profit = round(float(summary["gross_profit"]), 2)
    gross_loss = round(float(summary["gross_loss"]), 2)
    summary["realized_pnl"] = round(float(summary["realized_pnl"]), 2)
    summary["gross_profit"] = gross_profit
    summary["gross_loss"] = gross_loss
    summary["win_rate"] = round((wins * 100.0 / closed), 2) if closed else 0.0
    summary["average_pnl"] = round(summary["realized_pnl"] / closed, 2) if closed else 0.0
    summary["best_trade"] = round(float(summary["best_trade"]), 2) if summary["best_trade"] is not None else None
    summary["worst_trade"] = round(float(summary["worst_trade"]), 2) if summary["worst_trade"] is not None else None
    summary["no_loss_trade"] = bool(closed and gross_loss == 0)
    summary["profit_factor"] = round(gross_profit / abs(gross_loss), 2) if gross_loss < 0 else None
    return summary


def _load_rows(conn, user_id: int, requested_mode: str):
    """Load the authoritative ledger for the selected app mode.

    PAPER lives in paper_trades. LIVE local-gateway orders live in trades.
    The old implementation always read paper_trades, which is why the LIVE
    report card kept showing the historical 82 paper trades.
    """
    rows = []
    if requested_mode in ("all", "paper"):
        try:
            paper_rows = conn.execute(
                "SELECT * FROM paper_trades WHERE user_id=? ORDER BY id ASC",
                (int(user_id),),
            ).fetchall()
            rows.extend((row, "paper") for row in paper_rows)
        except Exception:
            pass

    if requested_mode in ("all", "live"):
        try:
            live_rows = conn.execute(
                "SELECT * FROM trades WHERE user_id=? ORDER BY id ASC",
                (int(user_id),),
            ).fetchall()
            rows.extend((row, "live") for row in live_rows)
        except Exception:
            pass

    return rows


def build_index_report_card(
    conn,
    user_id: int,
    mode: str = "all",
    now: datetime | None = None,
) -> dict:
    requested_mode = str(mode or "all").lower().strip()
    if requested_mode not in ("all", "paper", "live"):
        requested_mode = "all"

    rows = _load_rows(conn, user_id, requested_mode)

    summaries = {instrument: _empty_summary(instrument) for instrument in INSTRUMENTS}
    unclassified_trades = 0
    included_trades = 0

    for row, source_mode in rows:
        row_mode = _mode(row, source_mode)
        # Table is authoritative. A legacy/missing trading_mode field must not
        # cause a LIVE row from trades to be treated as PAPER.
        if source_mode == "live":
            row_mode = "live"
        elif source_mode == "paper":
            row_mode = "paper"

        if requested_mode != "all" and row_mode != requested_mode:
            continue

        instrument = _instrument(row)
        if instrument not in summaries:
            unclassified_trades += 1
            continue

        summary = summaries[instrument]
        summary["total_trades"] += 1
        included_trades += 1

        status = str(_value(row, "status", "") or "").upper()
        if status in {"OPEN", "PENDING", "EXIT_PENDING"}:
            summary["open_trades"] += 1
            continue
        if status != "CLOSED":
            continue

        pnl = _closed_pnl(row)
        summary["closed_trades"] += 1
        summary["realized_pnl"] += pnl
        summary["best_trade"] = pnl if summary["best_trade"] is None else max(float(summary["best_trade"]), pnl)
        summary["worst_trade"] = pnl if summary["worst_trade"] is None else min(float(summary["worst_trade"]), pnl)
        if pnl > 0:
            summary["profit_trades"] += 1
            summary["gross_profit"] += pnl
        elif pnl < 0:
            summary["loss_trades"] += 1
            summary["gross_loss"] += pnl
        else:
            summary["breakeven_trades"] += 1

    results = [_finalize(summaries[instrument]) for instrument in INSTRUMENTS]
    ranked = sorted(
        [item for item in results if item["closed_trades"] > 0],
        key=lambda item: (
            item["realized_pnl"],
            item["average_pnl"],
            item["win_rate"],
            item["closed_trades"],
        ),
        reverse=True,
    )
    for rank, item in enumerate(ranked, start=1):
        summaries[item["instrument"]]["rank"] = rank
        summaries[item["instrument"]]["is_best"] = rank == 1

    best = ranked[0] if ranked else None
    closed_sample = int(best["closed_trades"]) if best else 0
    confidence = (
        "HIGH" if closed_sample >= 20 else
        "MEDIUM" if closed_sample >= 5 else
        "LOW" if closed_sample else
        "NO_DATA"
    )
    total_realized = round(sum(item["realized_pnl"] for item in results), 2)
    generated = (now or datetime.now(timezone.utc)).astimezone(IST)

    return {
        "version": VERSION,
        "source": (
            "LIVE_TRADES_DB_ALL_TIME_NET_PNL" if requested_mode == "live" else
            "PAPER_TRADES_DB_ALL_TIME_NET_PNL" if requested_mode == "paper" else
            "PAPER_AND_LIVE_DB_ALL_TIME_NET_PNL"
        ),
        "pnl_basis": "REALIZED_NET_AFTER_EXECUTION_COSTS",
        "scope": "ALL_TIME",
        "mode": requested_mode,
        "generated_at_ist": generated.isoformat(),
        "indices": results,
        "best_index": best["instrument"] if best else None,
        "best_index_reason": "HIGHEST_REALIZED_NET_PNL" if best else "NO_CLOSED_TRADES",
        "comparison_confidence": confidence,
        "total_trades": included_trades,
        "closed_trades": sum(item["closed_trades"] for item in results),
        "open_trades": sum(item["open_trades"] for item in results),
        "total_realized_pnl": total_realized,
        "unclassified_trades": unclassified_trades,
        "display_mode": requested_mode.upper(),
        "empty_message": (
            "No live trades yet." if requested_mode == "live" else
            "No paper trades yet." if requested_mode == "paper" else
            "No trades yet."
        ),
    }


__all__ = ["INSTRUMENTS", "VERSION", "build_index_report_card"]
