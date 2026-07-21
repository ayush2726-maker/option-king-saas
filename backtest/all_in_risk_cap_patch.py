"""All-in refinement for the backtest 1% risk cap.

The base risk patch caps the premium move.  This refinement also includes
conservative slippage, brokerage and statutory charges when deciding how many
lots fit inside 1% of current equity.
"""

from backtest import cost_safe_breakeven_risk_patch as base
from backtest.realism_costs_patch import calculate_option_round_trip_costs


def _all_in_risk_capped_trade(trade, result, broker_name, equity):
    output = base._risk_capped_trade(
        trade,
        result,
        broker_name,
        equity,
    )
    if output.get("risk_cap_skipped"):
        return output

    instrument = str(output.get("instrument") or "NIFTY").upper()
    lot_size = max(1, base._i(output.get("lot_size"), 1))
    entry = max(base.TICK_SIZE, base._f(output.get("entry_price"), base.TICK_SIZE))
    hard_sl = max(
        base.TICK_SIZE,
        base._f(output.get("initial_sl_price"), entry),
    )
    max_loss = max(0.0, base._f(equity) * base.MAX_EQUITY_RISK_PERCENT / 100.0)

    one_lot = calculate_option_round_trip_costs(
        broker_name,
        instrument,
        entry,
        hard_sl,
        lot_size,
    )
    all_in_risk_per_lot = max(
        base.TICK_SIZE * lot_size,
        -base._f(one_lot.get("net_pnl"), 0.0),
    )
    allowed_lots = (
        int(max_loss // all_in_risk_per_lot)
        if all_in_risk_per_lot > 0
        else 0
    )
    allowed_lots = min(
        base._i(output.get("lots"), 0),
        allowed_lots,
    )

    if allowed_lots < 1:
        output.update({
            "risk_cap_skipped": True,
            "risk_cap_skip_reason": (
                "ONE_LOT_ALL_IN_RISK_EXCEEDS_1PCT_EQUITY"
            ),
            "qty": 0,
            "lots": 0,
            "pnl": 0.0,
            "all_in_risk_per_lot_rupees": round(
                all_in_risk_per_lot,
                2,
            ),
            "max_loss_rupees": round(max_loss, 2),
        })
        return output

    qty = allowed_lots * lot_size
    exit_price = max(
        base.TICK_SIZE,
        base._f(output.get("exit_price"), base.TICK_SIZE),
    )
    costs = calculate_option_round_trip_costs(
        broker_name,
        instrument,
        entry,
        exit_price,
        qty,
    )
    true_be = base.calculate_cost_safe_breakeven_price(
        broker_name,
        instrument,
        entry,
        qty,
        base.NET_PROFIT_LOCK_PERCENT,
    )

    output.update({
        "qty": qty,
        "lots": allowed_lots,
        "capital_used": round(entry * qty, 2),
        "used_capital": round(entry * qty, 2),
        "all_in_risk_per_lot_rupees": round(
            all_in_risk_per_lot,
            2,
        ),
        "expected_max_loss_after_all_costs": round(
            all_in_risk_per_lot * allowed_lots,
            2,
        ),
        "max_loss_rupees": round(max_loss, 2),
        "risk_cap_applied": bool(
            output.get("risk_cap_applied")
            or qty < base._i(output.get("qty_before_risk_cap"), qty)
        ),
        "cost_safe_breakeven_price": true_be["price"],
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
    })
    return output


def apply_all_in_risk_cap_patch():
    if getattr(base, "_okai_all_in_risk_cap_v1", False):
        return
    base._risk_capped_trade = _all_in_risk_capped_trade
    base._okai_all_in_risk_cap_v1 = True
