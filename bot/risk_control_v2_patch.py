"""OKAI Risk Control V2.

The score remains 82 and entry-quality rules remain unchanged. This patch limits
position size by planned ATR-stop risk instead of spending the whole 50%/40%
slot, and prevents repeated same-index/same-side ATR-stop losses:

- Slot 1 planned risk: at most 1.25% of current equity.
- Slot 2 planned risk: at most 0.75% of current equity.
- The existing 50%/40% allocation remains an affordability ceiling only.
- After the first PURE ATR SL, the same index+side waits 30 minutes.
- After the second PURE ATR SL on the same index+side in a day, it is blocked
  for the rest of that trading day.
- Backtest uses the same sizing and post-loss rules.

PAPER unlimited observation and LIVE maximum-five rules are not changed.
"""

import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from backtest import post_loss_reentry_cooldown_patch as backtest_reentry
from backtest import routes as backtest_routes
from bot import auto_portfolio_runtime as runtime
from bot import dynamic_exit
from bot import post_loss_reentry_guard_patch as runtime_reentry


RISK_PERCENT_BY_SLOT = {1: 1.25, 2: 0.75}
FIRST_LOSS_COOLDOWN_MINUTES = 30
FIRST_LOSS_REASON = "POST_ATR_SL_SAME_SIDE_COOLDOWN_30M"
SECOND_LOSS_REASON = "POST_ATR_SL_SAME_SIDE_BLOCK_REST_OF_DAY_AFTER_2"
PLANNED_PREMIUM_RISK_PERCENT = float(dynamic_exit.MAX_PREMIUM_RISK_PERCENT)


def _f(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _i(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def _risk_percent_for_slot(slot):
    return float(RISK_PERCENT_BY_SLOT.get(_i(slot, 1), 0.75))


def _risk_lots(capital_base, slot, premium, lot_size):
    capital = max(0.0, _f(capital_base))
    price = max(0.05, _f(premium, 0.05))
    lot = max(1, _i(lot_size, 1))
    risk_percent = _risk_percent_for_slot(slot)
    risk_budget = capital * risk_percent / 100.0
    planned_risk_per_lot = (
        price * PLANNED_PREMIUM_RISK_PERCENT / 100.0 * lot
    )
    lots = (
        int(math.floor(risk_budget / planned_risk_per_lot))
        if planned_risk_per_lot > 0
        else 0
    )
    return {
        "lots": max(0, lots),
        "risk_percent": risk_percent,
        "risk_budget": round(risk_budget, 2),
        "planned_risk_per_lot": round(planned_risk_per_lot, 2),
    }


def _install_runtime_risk_sizing():
    if getattr(runtime, "_okai_risk_sizing_v2", False):
        return

    original_size = runtime._size

    def risk_capped_size(capital_base, slot, premium, lot_size):
        base = dict(original_size(capital_base, slot, premium, lot_size) or {})
        risk = _risk_lots(capital_base, slot, premium, lot_size)
        original_lots = max(0, _i(base.get("lots"), 0))
        allowed_lots = min(original_lots, risk["lots"])
        lot = max(1, _i(lot_size, 1))
        qty = allowed_lots * lot
        price = max(0.0, _f(premium))

        base.update({
            "lots": allowed_lots,
            "qty": qty,
            "capital_used": round(price * qty, 2),
            "risk_cap_applied": allowed_lots < original_lots,
            "risk_sizing_mode": "EQUITY_RISK_CAP_V2",
            "max_planned_loss_percent": risk["risk_percent"],
            "max_planned_loss_amount": risk["risk_budget"],
            "planned_risk_per_lot": risk["planned_risk_per_lot"],
            "affordability_lots": original_lots,
            "risk_lots": risk["lots"],
        })
        return base

    runtime._size = risk_capped_size
    runtime._okai_risk_sizing_v2 = True


def _install_backtest_risk_sizing():
    if getattr(backtest_routes, "_okai_risk_sizing_v2", False):
        return

    original_sizing = getattr(
        backtest_routes,
        "_okai_calculate_lot_sizing",
        None,
    )
    if callable(original_sizing):
        def risk_capped_backtest_sizing(
            capital,
            premium,
            lot_size,
            allocation,
        ):
            base = dict(
                original_sizing(
                    capital,
                    premium,
                    lot_size,
                    allocation,
                ) or {}
            )
            slot = 1 if _f(allocation) >= 0.49 else 2
            risk = _risk_lots(capital, slot, premium, lot_size)
            original_lots = max(0, _i(base.get("lots"), 0))
            allowed_lots = min(original_lots, risk["lots"])
            lot = max(1, _i(lot_size, 1))
            quantity = allowed_lots * lot
            capital_used = round(_f(premium) * quantity, 2)
            capital_value = max(0.01, _f(capital, 0.01))

            base.update({
                "lots": allowed_lots,
                "quantity": quantity,
                "capital_used": capital_used,
                "capital_utilization_percent": round(
                    capital_used / capital_value * 100.0,
                    2,
                ),
                "affordable": allowed_lots >= 1,
                "risk_cap_applied": allowed_lots < original_lots,
                "risk_sizing_mode": "EQUITY_RISK_CAP_V2",
                "max_planned_loss_percent": risk["risk_percent"],
                "max_planned_loss_amount": risk["risk_budget"],
                "planned_risk_per_lot": risk["planned_risk_per_lot"],
                "affordability_lots": original_lots,
                "risk_lots": risk["lots"],
            })
            return base

        backtest_routes._okai_calculate_lot_sizing = (
            risk_capped_backtest_sizing
        )

    original_auto = getattr(
        backtest_routes,
        "_okai_run_auto_index_backtest",
        None,
    )
    if callable(original_auto):
        def risk_annotated_auto(*args, **kwargs):
            result = original_auto(*args, **kwargs)
            if not isinstance(result, dict):
                return result
            output = result
            output["position_sizing"] = {
                **dict(output.get("position_sizing") or {}),
                "mode": "LIVE_SLOT_CEILING_PLUS_EQUITY_RISK_CAP_V2",
                "slot_1_max_planned_loss_percent": 1.25,
                "slot_2_max_planned_loss_percent": 0.75,
                "max_combined_planned_open_risk_percent": 2.0,
                "premium_stop_risk_basis_percent": (
                    PLANNED_PREMIUM_RISK_PERCENT
                ),
            }
            output["risk_control_version"] = "OKAI_RISK_CONTROL_V2"
            output["note"] = (
                "AUTO keeps 50%/40% slot ceilings, but lots are capped so "
                "planned ATR-stop loss is at most 1.25% for slot 1 and "
                "0.75% for slot 2."
            )
            summary = dict(output.get("summary") or {})
            summary.update({
                "risk_control_version": "OKAI_RISK_CONTROL_V2",
                "slot_1_max_planned_loss_percent": 1.25,
                "slot_2_max_planned_loss_percent": 0.75,
            })
            output["summary"] = summary
            return output

        backtest_routes._okai_run_auto_index_backtest = risk_annotated_auto

    backtest_routes._okai_risk_sizing_v2 = True


def _now_utc():
    return datetime.now(timezone.utc)


def _ist_day_bounds_utc(now=None):
    current = now or _now_utc()
    ist = timezone(timedelta(hours=5, minutes=30))
    current_ist = current.astimezone(ist)
    start_ist = current_ist.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    end_ist = start_ist + timedelta(days=1)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def _runtime_register_loss_block_v2(conn, user_id, trade, reason):
    text = str(reason or "").upper()
    if "PURE ATR SL" not in text:
        return

    underlying = runtime._underlying(trade)
    side = str(runtime._v(trade, "side", "") or "").upper()
    if side not in ("CE", "PE"):
        return

    runtime_reentry._ensure_guard_schema(conn)
    now = _now_utc()
    start_utc, end_utc = _ist_day_bounds_utc(now)

    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM paper_trades
        WHERE user_id=?
          AND status='CLOSED'
          AND datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
          AND UPPER(COALESCE(underlying, ''))=?
          AND UPPER(COALESCE(side, ''))=?
          AND UPPER(COALESCE(reason, '')) LIKE '%PURE ATR SL%'
        """,
        (
            int(user_id),
            start_utc.strftime("%Y-%m-%d %H:%M:%S"),
            end_utc.strftime("%Y-%m-%d %H:%M:%S"),
            underlying,
            side,
        ),
    ).fetchone()
    loss_count = max(1, _i(row["c"] if row else 1, 1))

    if loss_count >= 2:
        blocked_until = end_utc
        block_reason = SECOND_LOSS_REASON
    else:
        blocked_until = now + timedelta(
            minutes=FIRST_LOSS_COOLDOWN_MINUTES
        )
        block_reason = FIRST_LOSS_REASON

    conn.execute(
        """
        INSERT INTO auto_reentry_blocks (
            user_id, underlying, side, blocked_until,
            source_trade_id, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, underlying, side)
        DO UPDATE SET
            blocked_until=excluded.blocked_until,
            source_trade_id=excluded.source_trade_id,
            reason=excluded.reason,
            created_at=excluded.created_at
        """,
        (
            int(user_id),
            underlying,
            side,
            runtime_reentry._iso(blocked_until),
            runtime._i(runtime._v(trade, "id", 0), 0),
            block_reason,
            runtime_reentry._iso(now),
        ),
    )
    conn.commit()


def _runtime_block_scan_v2(scan, block):
    if (
        not isinstance(scan, dict)
        or str(scan.get("status") or "").upper() != "OK"
    ):
        return scan

    signal = dict(scan.get("signal_data") or {})
    candidate = str(
        signal.get("candidate_signal")
        or signal.get("signal")
        or "WAIT"
    ).upper()
    if candidate not in ("CE", "PE"):
        return scan

    reason = str((block or {}).get("reason") or FIRST_LOSS_REASON)
    reasons = list(signal.get("safety_gate_reasons") or [])
    fresh = list(signal.get("fresh_entry_block_reasons") or [])
    warnings = list(signal.get("warnings") or [])
    for collection in (reasons, fresh, warnings):
        if reason not in collection:
            collection.append(reason)

    signal.update({
        "signal": "WAIT",
        "trade_allowed": False,
        "safety_gate_passed": False,
        "fresh_entry_ok": False,
        "safety_gate_reasons": reasons,
        "fresh_entry_block_reasons": fresh,
        "warnings": warnings,
        "post_loss_reentry_blocked": True,
        "post_loss_reentry_reason": reason,
        "post_loss_reentry_blocked_until": (block or {}).get(
            "blocked_until"
        ),
        "post_loss_source_trade_id": (block or {}).get(
            "source_trade_id"
        ),
    })
    scan["signal_data"] = signal
    market = dict(scan.get("market_data") or {})
    market["signal"] = "WAIT"
    scan["market_data"] = market
    scan["entry_block_reason"] = reason
    return scan


def _install_runtime_post_loss_v2():
    runtime_reentry.COOLDOWN_SECONDS = (
        FIRST_LOSS_COOLDOWN_MINUTES * 60
    )
    runtime_reentry.BLOCK_REASON = FIRST_LOSS_REASON
    runtime_reentry._register_loss_block = (
        _runtime_register_loss_block_v2
    )
    runtime_reentry._block_scan = _runtime_block_scan_v2
    runtime._okai_post_loss_reentry_guard_v2 = True


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def _is_atr_stop(trade):
    text = str((trade or {}).get("reason") or "").upper()
    return "PURE_ATR_SL" in text or "PURE ATR SL" in text


def _backtest_filter_result_v2(result):
    if not isinstance(result, dict) or not result.get("success"):
        return result

    source = list(result.get("trades") or [])
    if not source:
        output = deepcopy(result)
        output["post_loss_reentry_cooldown_minutes"] = (
            FIRST_LOSS_COOLDOWN_MINUTES
        )
        output["post_loss_reentry_model"] = (
            "30M_THEN_REST_OF_DAY_AFTER_SECOND_ATR_SL"
        )
        return output

    ordered = sorted(
        source,
        key=lambda row: _parse_time(row.get("entry_time")),
    )
    blocked_until = {}
    blocked_reason = {}
    atr_loss_count = {}
    kept = []
    blocked = []

    for row in ordered:
        trade = dict(row)
        side = str(trade.get("side") or "").upper()
        entry_time = _parse_time(trade.get("entry_time"))
        until = blocked_until.get(side)

        if side in ("CE", "PE") and until is not None and entry_time < until:
            reason = blocked_reason.get(side, FIRST_LOSS_REASON)
            trade["backtest_block_reason"] = reason
            trade["backtest_blocked_until"] = until.isoformat()
            blocked.append(trade)
            continue

        trade["trade_no"] = len(kept) + 1
        kept.append(trade)

        if _is_atr_stop(trade) and side in ("CE", "PE"):
            atr_loss_count[side] = atr_loss_count.get(side, 0) + 1
            exit_time = _parse_time(trade.get("exit_time"))
            if exit_time == datetime.min:
                continue
            if atr_loss_count[side] >= 2:
                blocked_until[side] = exit_time.replace(
                    hour=23,
                    minute=59,
                    second=59,
                    microsecond=999999,
                )
                blocked_reason[side] = SECOND_LOSS_REASON
            else:
                blocked_until[side] = exit_time + timedelta(
                    minutes=FIRST_LOSS_COOLDOWN_MINUTES
                )
                blocked_reason[side] = FIRST_LOSS_REASON

    output = backtest_reentry._recalculate(result, kept)
    output["post_loss_reentry_cooldown_minutes"] = (
        FIRST_LOSS_COOLDOWN_MINUTES
    )
    output["post_loss_reentry_model"] = (
        "30M_THEN_REST_OF_DAY_AFTER_SECOND_ATR_SL"
    )
    output["post_loss_reentries_blocked"] = len(blocked)
    output["blocked_post_loss_reentries"] = blocked
    output["same_side_atr_loss_counts"] = atr_loss_count
    return output


def _install_backtest_post_loss_v2():
    backtest_reentry.COOLDOWN_MINUTES = FIRST_LOSS_COOLDOWN_MINUTES
    backtest_reentry.COOLDOWN_REASON = FIRST_LOSS_REASON
    backtest_reentry._filter_result = _backtest_filter_result_v2
    backtest_routes._okai_backtest_post_loss_cooldown_v3 = True


def apply_risk_control_v2_patch():
    if getattr(runtime, "_okai_risk_control_v2", False):
        return

    _install_runtime_risk_sizing()
    _install_runtime_post_loss_v2()
    _install_backtest_risk_sizing()
    _install_backtest_post_loss_v2()

    runtime._okai_risk_control_v2 = True
