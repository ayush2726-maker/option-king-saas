"""Quantity-preserving risk refinement for backtests.

The upstream AUTO/monthly engine decides quantity from the configured capital
allocation.  This patch must not reduce those lots.  It only:

* limits the initial option-premium stop distance to 8%;
* makes the hard stop take precedence over a later modeled exit below it;
* keeps the true break-even rule at entry + all costs + 2% net profit; and
* recalculates P&L after conservative slippage and statutory costs.

The earlier 1%-of-equity sizing cap was intentionally removed because it
silently changed CAP90 / portfolio quantities into only one to three lots.
"""

from backtest import cost_safe_breakeven_risk_patch as base
from backtest.realism_costs_patch import calculate_option_round_trip_costs


_ORIGINAL_RECALCULATE_RESULT = base._recalculate_result


def _instrument_of(trade, result):
    instrument = str(
        trade.get("instrument")
        or trade.get("underlying")
        or result.get("instrument")
        or "NIFTY"
    ).upper()
    if instrument == "AUTO":
        instrument = str(
            trade.get("selected_instrument")
            or trade.get("symbol", "NIFTY").split()[0]
            or "NIFTY"
        ).upper()
    return instrument


def _quantity_preserving_trade(trade, result, broker_name, equity):
    output = dict(trade)
    instrument = _instrument_of(output, result)
    lot_size = max(
        1,
        base._i(
            output.get("lot_size"),
            base.routes.LOT_SIZES.get(instrument, 1),
        ),
    )
    original_qty = max(lot_size, base._i(output.get("qty"), lot_size))
    original_lots = max(
        1,
        base._i(output.get("lots"), original_qty // lot_size),
    )
    qty = original_lots * lot_size

    entry = max(
        base.TICK_SIZE,
        base._f(output.get("entry_price"), base.TICK_SIZE),
    )
    original_exit = max(
        base.TICK_SIZE,
        base._f(output.get("exit_price"), base.TICK_SIZE),
    )
    atr_risk = max(
        base.TICK_SIZE,
        base._f(output.get("risk_points"), entry * 0.08),
    )
    premium_risk_cap = max(
        base.TICK_SIZE,
        entry * base.MAX_PREMIUM_RISK_PERCENT / 100.0,
    )
    risk_points = min(
        atr_risk,
        premium_risk_cap,
        max(base.TICK_SIZE, entry - base.TICK_SIZE),
    )
    hard_sl = round(max(base.TICK_SIZE, entry - risk_points), 2)

    adjusted_exit = original_exit
    reason = str(output.get("reason") or "").upper()
    if adjusted_exit + 1e-9 < hard_sl:
        output["original_exit_price_before_hard_stop"] = round(
            adjusted_exit,
            2,
        )
        output["original_reason_before_hard_stop"] = output.get("reason")
        adjusted_exit = hard_sl
        reason = "HARD_PREMIUM_RISK_CAP_SL"
        output["hard_stop_precedence_applied"] = True
        output["stop_execution_model"] = (
            "8PCT_PREMIUM_HARD_STOP_PRECEDES_REVERSAL_OR_EOD"
        )

    true_be = base.calculate_cost_safe_breakeven_price(
        broker_name,
        instrument,
        entry,
        qty,
        base.NET_PROFIT_LOCK_PERCENT,
    )
    be_price = base._f(true_be.get("price"), entry)
    peak_price = max(
        entry,
        base._f(output.get("peak_price"), entry),
    )

    if (
        "PROFIT_LOCK" in reason
        and peak_price + 1e-9 >= be_price
        and adjusted_exit + 1e-9 < be_price
    ):
        output["original_exit_price_before_true_be"] = round(
            adjusted_exit,
            2,
        )
        adjusted_exit = be_price
        reason = "TRUE_BE_PLUS_2PCT_TRAIL"

    costs = calculate_option_round_trip_costs(
        broker_name,
        instrument,
        entry,
        adjusted_exit,
        qty,
    )
    one_lot_stop = calculate_option_round_trip_costs(
        broker_name,
        instrument,
        entry,
        hard_sl,
        lot_size,
    )
    all_in_risk_per_lot = max(
        base.TICK_SIZE * lot_size,
        -base._f(one_lot_stop.get("net_pnl"), 0.0),
    )
    expected_stop_loss = all_in_risk_per_lot * original_lots

    output.update({
        "instrument": instrument,
        "lot_size": lot_size,
        "qty_before_risk_cap": original_qty,
        "lots_before_risk_cap": original_lots,
        "qty": qty,
        "lots": original_lots,
        "capital_used": round(entry * qty, 2),
        "used_capital": round(entry * qty, 2),
        "entry_price": round(entry, 2),
        "exit_price": round(adjusted_exit, 2),
        "reason": reason or output.get("reason"),
        "initial_sl_price": hard_sl,
        "sl_price": round(
            max(hard_sl, base._f(output.get("sl_price"), hard_sl)),
            2,
        ),
        "risk_points_before_cap": round(atr_risk, 2),
        "risk_points": round(risk_points, 2),
        "risk_points_after_cap": round(risk_points, 2),
        "premium_risk_cap_percent": base.MAX_PREMIUM_RISK_PERCENT,
        "equity_risk_cap_percent": None,
        "quantity_preserved": True,
        "quantity_risk_cap_enabled": False,
        "quantity_sizing_rule": "PRESERVE_UPSTREAM_CAPITAL_ALLOCATION",
        "stop_risk_cap_enabled": True,
        "stop_risk_cap_rule": "MAX_8PCT_OPTION_PREMIUM_DISTANCE",
        "all_in_risk_per_lot_rupees": round(all_in_risk_per_lot, 2),
        "expected_stop_loss_after_all_costs": round(
            expected_stop_loss,
            2,
        ),
        "risk_cap_applied": bool(
            risk_points + 1e-9 < atr_risk
            or adjusted_exit != original_exit
        ),
        "risk_cap_skipped": False,
        "cost_safe_breakeven_price": true_be["price"],
        "breakeven_rule": "ENTRY_PLUS_ALL_COSTS_PLUS_2PCT_NET",
        "breakeven_target_net_profit": true_be[
            "target_net_profit"
        ],
        "breakeven_net_pnl_at_stop": true_be[
            "net_pnl_at_price"
        ],
        "breakeven_total_charges": true_be[
            "total_charges_at_price"
        ],
        "gross_pnl": costs["market_gross_pnl"],
        "slippage_cost": costs["slippage_cost"],
        "charges": costs,
        "total_charges": costs["total_charges"],
        "pnl": costs["net_pnl"],
        "pnl_after_all_costs": costs["net_pnl"],
        "cost_model": "INDIA_INDEX_OPTIONS_ALL_COSTS_V2",
    })
    return output


def _recalculate_with_quantity_preserved(result, broker_name):
    output = _ORIGINAL_RECALCULATE_RESULT(result, broker_name)
    if not isinstance(output, dict) or not output.get("success"):
        return output

    metadata = {
        "risk_cap_enabled": True,
        "risk_cap_rule": "8PCT_PREMIUM_STOP_ONLY_QUANTITY_UNCHANGED",
        "quantity_risk_cap_enabled": False,
        "quantity_preserved": True,
        "capital_use_rule": "PRESERVE_UPSTREAM_CAP90_OR_PORTFOLIO_SLOT",
        "cost_safe_be_risk_patch": "V2_QUANTITY_PRESERVED",
    }
    output.update(metadata)
    summary = dict(output.get("summary") or {})
    summary.update(metadata)
    output["summary"] = summary
    return output


def apply_all_in_risk_cap_patch():
    if getattr(base, "_okai_all_in_risk_cap_v2", False):
        return

    base._risk_capped_trade = _quantity_preserving_trade
    base._recalculate_result = _recalculate_with_quantity_preserved
    base._okai_all_in_risk_cap_v1 = True
    base._okai_all_in_risk_cap_v2 = True
