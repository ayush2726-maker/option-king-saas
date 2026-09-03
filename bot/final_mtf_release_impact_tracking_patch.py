"""Track actual trades opened after releasing the legacy FINAL VWAP/ST/EMA/MTF veto.

Observation only: this module does not change entry, exit, sizing, score or risk logic.
It tags entry audit snapshots when the targeted release was required, then adds an
actual closed/open trade impact report to the existing missed-trade AI payload.
"""
from __future__ import annotations

import json
from typing import Any

from database import get_db
from bot import entry_execution_safety_v1_patch as safety
from bot import missed_trade_learning_v1 as missed
from bot import ai_routes

VERSION = "FINAL_MTF_RELEASE_IMPACT_TRACKING_V1"


def _b(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _loads(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(str(value or "{}"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _impact(user_id: int) -> dict[str, Any]:
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT id,symbol,underlying,trading_mode,entry_price,exit_price,qty,pnl,
                   status,reason,created_at,entry_context_json
            FROM paper_trades
            WHERE user_id=? AND entry_context_json IS NOT NULL
            ORDER BY id DESC
            LIMIT 300
            """,
            (int(user_id),),
        ).fetchall()
    except Exception:
        return {
            "version": VERSION,
            "tracked": 0,
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "flat": 0,
            "open": 0,
            "total_pnl": 0.0,
            "trades": [],
        }
    finally:
        conn.close()

    tracked = []
    for raw in rows:
        row = dict(raw)
        context = _loads(row.get("entry_context_json"))
        if not _b(context.get("final_mtf_misaligned_release_applied")):
            continue
        pnl = _f(row.get("pnl"), 0.0)
        status = str(row.get("status") or "").upper()
        if status == "OPEN":
            outcome = "OPEN"
        elif pnl > 0.01:
            outcome = "PROFIT"
        elif pnl < -0.01:
            outcome = "LOSS"
        else:
            outcome = "FLAT"
        tracked.append({
            "trade_id": row.get("id"),
            "symbol": row.get("symbol"),
            "underlying": row.get("underlying"),
            "mode": str(row.get("trading_mode") or "paper").upper(),
            "entry_price": row.get("entry_price"),
            "exit_price": row.get("exit_price"),
            "qty": _i(row.get("qty"), 0),
            "pnl": round(pnl, 2),
            "status": status,
            "outcome": outcome,
            "entry_time": row.get("created_at"),
            "exit_reason": row.get("reason"),
            "release_version": context.get("final_mtf_misaligned_release_version"),
            "original_block": context.get("final_mtf_original_block_reason") or "FINAL VWAP ST EMA MTF MISALIGNED",
        })

    closed = [t for t in tracked if t["outcome"] != "OPEN"]
    wins = [t for t in closed if t["outcome"] == "PROFIT"]
    losses = [t for t in closed if t["outcome"] == "LOSS"]
    flats = [t for t in closed if t["outcome"] == "FLAT"]
    opens = [t for t in tracked if t["outcome"] == "OPEN"]
    total_pnl = round(sum(_f(t.get("pnl"), 0.0) for t in closed), 2)

    return {
        "version": VERSION,
        "block_removed": "FINAL VWAP ST EMA MTF MISALIGNED",
        "tracked": len(tracked),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(flats),
        "open": len(opens),
        "win_rate_percent": round(len(wins) / len(closed) * 100.0, 1) if closed else 0.0,
        "total_pnl": total_pnl,
        "trades": tracked[:30],
    }


def apply_final_mtf_release_impact_tracking_patch() -> None:
    if getattr(safety, "_okai_final_mtf_release_impact_tracking_v1", False):
        return

    original_snapshot = safety._entry_snapshot

    def snapshot_with_release_tag(broker_name, selected, resolved, quote_price, quality, momentum, candle, health):
        snapshot = dict(original_snapshot(
            broker_name, selected, resolved, quote_price, quality, momentum, candle, health
        ) or {})
        signal = dict((selected or {}).get("signal_data") or {})
        released = _b(signal.get("final_mtf_misaligned_release_applied"))
        snapshot["final_mtf_misaligned_release_applied"] = released
        if released:
            snapshot["final_mtf_misaligned_release_version"] = signal.get(
                "final_mtf_misaligned_release_version"
            )
            snapshot["final_mtf_original_block_reason"] = "FINAL VWAP ST EMA MTF MISALIGNED"
        return snapshot

    safety._entry_snapshot = snapshot_with_release_tag

    original_summary = missed.get_missed_trade_summary

    def summary_with_release_impact(user_id, *args, **kwargs):
        report = original_summary(user_id, *args, **kwargs)
        if isinstance(report, dict):
            report["final_mtf_release_impact"] = _impact(int(user_id))
        return report

    missed.get_missed_trade_summary = summary_with_release_impact
    ai_routes.get_missed_trade_summary = summary_with_release_impact
    safety._okai_final_mtf_release_impact_tracking_v1 = True


__all__ = ["apply_final_mtf_release_impact_tracking_patch", "_impact", "VERSION"]
