"""Per-block counterfactual performance for the Advanced AI missed-trade report.

Read-only analytics. This module never changes strategy decisions or order flow.
"""
from __future__ import annotations

import json
from collections import defaultdict

from database import get_db


def _loads(value):
    try:
        data = json.loads(str(value or "[]"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _candidate_pnl(row):
    side = str(row.get("candidate_side") or "").upper()
    if side == "CE":
        return row.get("ce_net_pnl")
    if side == "PE":
        return row.get("pe_net_pnl")
    return None


def _recommendation(samples, missed_profit, saved_loss, net_if_taken):
    if samples < 10:
        return "COLLECT"
    if net_if_taken <= 0:
        return "KEEP"
    if saved_loss == 0 and samples >= 20 and missed_profit >= 15:
        return "REMOVE"
    return "RELAX"


def get_block_performance(user_id: int, horizon_minutes: int = 15):
    """Aggregate each recorded strategy block reason against exact option P&L."""
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT m.id,m.underlying,m.candidate_side,m.block_stage,
                   m.block_reasons_json,m.advanced_decision_id,
                   o.ce_net_pnl,o.pe_net_pnl,o.training_eligible
            FROM ai_missed_trade_signals_v1 m
            JOIN ai_advanced_v2_contract_outcomes o
              ON o.decision_id=m.advanced_decision_id
             AND o.horizon_minutes=?
            WHERE m.user_id=?
              AND m.decision_kind='STRATEGY_BLOCKED'
            ORDER BY datetime(m.created_at),m.rowid
            """,
            (int(horizon_minutes), int(user_id)),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    stats = defaultdict(lambda: {
        "total_blocked": 0,
        "evaluated": 0,
        "profit_missed": 0,
        "loss_saved": 0,
        "flat": 0,
        "net_if_taken": 0.0,
        "nifty": 0,
        "sensex": 0,
    })

    for raw in rows:
        row = dict(raw)
        pnl = _candidate_pnl(row)
        if pnl is None:
            continue
        try:
            pnl = float(pnl)
        except Exception:
            continue
        reasons = _loads(row.get("block_reasons_json"))
        if not reasons:
            reasons = [row.get("block_stage") or "UNSPECIFIED_BLOCK"]
        unique = []
        for value in reasons:
            reason = str(value or "").strip().upper()
            if reason and reason not in unique:
                unique.append(reason)
        for reason in unique:
            item = stats[reason]
            item["total_blocked"] += 1
            item["evaluated"] += 1
            item["net_if_taken"] += pnl
            underlying = str(row.get("underlying") or "").upper()
            if underlying == "NIFTY":
                item["nifty"] += 1
            elif underlying == "SENSEX":
                item["sensex"] += 1
            if pnl > 0.01:
                item["profit_missed"] += 1
            elif pnl < -0.01:
                item["loss_saved"] += 1
            else:
                item["flat"] += 1

    output = []
    for reason, item in stats.items():
        evaluated = int(item["evaluated"])
        net_if_taken = round(float(item["net_if_taken"]), 2)
        block_benefit = round(-net_if_taken, 2)
        recommendation = _recommendation(
            evaluated,
            int(item["profit_missed"]),
            int(item["loss_saved"]),
            net_if_taken,
        )
        output.append({
            "reason": reason,
            "horizon_minutes": int(horizon_minutes),
            **item,
            "net_if_taken": net_if_taken,
            "block_net_benefit": block_benefit,
            "profit_missed_percent": round(item["profit_missed"] / evaluated * 100, 1) if evaluated else 0.0,
            "loss_saved_percent": round(item["loss_saved"] / evaluated * 100, 1) if evaluated else 0.0,
            "recommendation": recommendation,
            "recommendation_basis": (
                "Need at least 10 evaluated samples" if recommendation == "COLLECT" else
                "Blocking avoided more net loss than it missed" if recommendation == "KEEP" else
                "Block has positive missed-trade expectancy but still saves some losses" if recommendation == "RELAX" else
                "Large sample shows positive missed-trade expectancy with no saved losses"
            ),
        })

    rank = {"REMOVE": 0, "RELAX": 1, "KEEP": 2, "COLLECT": 3}
    output.sort(key=lambda x: (rank.get(x["recommendation"], 9), -abs(float(x["net_if_taken"])), -int(x["evaluated"])))
    return output


def apply_block_performance_report_patch() -> None:
    """Attach block_performance to the existing /bot/ai-missed-trades payload."""
    from bot import missed_trade_learning_v1 as missed
    from bot import ai_routes

    if getattr(missed, "_okai_block_performance_v1", False):
        return

    original = missed.get_missed_trade_summary

    def wrapped(user_id, *args, **kwargs):
        report = original(user_id, *args, **kwargs)
        if not isinstance(report, dict):
            return report
        report["block_performance"] = get_block_performance(int(user_id), 15)
        report["block_performance_horizon_minutes"] = 15
        return report

    missed.get_missed_trade_summary = wrapped
    # ai_routes imported the function by name, so update that reference too.
    ai_routes.get_missed_trade_summary = wrapped
    missed._okai_block_performance_v1 = True
