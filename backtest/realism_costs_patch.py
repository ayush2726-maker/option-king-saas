"""Backtest Realism Costs V2.

Applies conservative fill slippage and complete Indian index-option round-trip
costs to Daily/AUTO/Monthly results.  Signal candles remain real one-minute
historical index candles.  Option premiums are still synthetic unless a future
expired-contract OHLC provider is available, so results are deliberately marked
as estimated rather than claimed to be exact live fills.
"""

from copy import deepcopy

from backtest import routes


# Current retail option defaults.  Brokerage is per executed order, therefore a
# completed long option trade normally has one BUY and one SELL order.
BROKERAGE_PER_ORDER = {
    "angelone": 20.0,
    "angel": 20.0,
    "zerodha": 20.0,
    "upstox": 20.0,
}

# Percent of premium turnover.
NSE_OPTION_TRANSACTION_PERCENT = 0.03553
BSE_SENSEX_OPTION_TRANSACTION_PERCENT = 0.03250
OPTION_STT_SELL_PERCENT = 0.15
OPTION_STAMP_BUY_PERCENT = 0.003
SEBI_PERCENT = 0.0001  # ₹10/crore
NSE_IPFT_PERCENT = 0.0000001
GST_PERCENT = 18.0

# Conservative market-fill approximation: at least one ₹0.05 tick and 0.10%
# adverse slippage on entry and exit.  Actual contract liquidity can differ.
SLIPPAGE_PERCENT_EACH_SIDE = 0.10
TICK_SIZE = 0.05


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _i(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def _brokerage(broker_name):
    return BROKERAGE_PER_ORDER.get(str(broker_name or "angelone").lower(), 20.0)


def _fill_prices(entry_price, exit_price):
    entry = max(TICK_SIZE, _f(entry_price, TICK_SIZE))
    exit_value = max(TICK_SIZE, _f(exit_price, TICK_SIZE))

    entry_slip = max(TICK_SIZE, entry * SLIPPAGE_PERCENT_EACH_SIDE / 100.0)
    exit_slip = max(TICK_SIZE, exit_value * SLIPPAGE_PERCENT_EACH_SIDE / 100.0)

    simulated_entry = round(entry + entry_slip, 2)
    simulated_exit = round(max(TICK_SIZE, exit_value - exit_slip), 2)
    return simulated_entry, simulated_exit


def calculate_option_round_trip_costs(
    broker_name,
    instrument,
    entry_price,
    exit_price,
    quantity,
):
    qty = max(1, _i(quantity, 1))
    simulated_entry, simulated_exit = _fill_prices(entry_price, exit_price)

    buy_turnover = simulated_entry * qty
    sell_turnover = simulated_exit * qty
    total_turnover = buy_turnover + sell_turnover

    brokerage = _brokerage(broker_name) * 2.0
    is_bse = str(instrument or "").upper() == "SENSEX"
    transaction_rate = (
        BSE_SENSEX_OPTION_TRANSACTION_PERCENT
        if is_bse
        else NSE_OPTION_TRANSACTION_PERCENT
    )
    transaction = total_turnover * transaction_rate / 100.0
    stt = sell_turnover * OPTION_STT_SELL_PERCENT / 100.0
    stamp = buy_turnover * OPTION_STAMP_BUY_PERCENT / 100.0
    sebi = total_turnover * SEBI_PERCENT / 100.0
    ipft = 0.0 if is_bse else total_turnover * NSE_IPFT_PERCENT / 100.0
    gst_base = brokerage + transaction + sebi + ipft
    gst = gst_base * GST_PERCENT / 100.0

    total_charges = brokerage + transaction + stt + stamp + sebi + ipft + gst
    original_gross = (_f(exit_price) - _f(entry_price)) * qty
    slippage_adjusted_gross = (simulated_exit - simulated_entry) * qty
    slippage_cost = original_gross - slippage_adjusted_gross
    net = slippage_adjusted_gross - total_charges

    return {
        "simulated_entry_price": round(simulated_entry, 2),
        "simulated_exit_price": round(simulated_exit, 2),
        "buy_turnover": round(buy_turnover, 2),
        "sell_turnover": round(sell_turnover, 2),
        "total_turnover": round(total_turnover, 2),
        "market_gross_pnl": round(original_gross, 2),
        "slippage_cost": round(slippage_cost, 2),
        "gross_pnl_after_slippage": round(slippage_adjusted_gross, 2),
        "brokerage": round(brokerage, 2),
        "transaction_charges": round(transaction, 2),
        "stt": round(stt, 2),
        "stamp_duty": round(stamp, 2),
        "sebi_charges": round(sebi, 2),
        "ipft": round(ipft, 4),
        "gst": round(gst, 2),
        "total_charges": round(total_charges, 2),
        "net_pnl": round(net, 2),
        "exchange": "BSE" if is_bse else "NSE",
        "transaction_rate_percent": transaction_rate,
        "stt_sell_percent": OPTION_STT_SELL_PERCENT,
        "stamp_buy_percent": OPTION_STAMP_BUY_PERCENT,
        "slippage_percent_each_side": SLIPPAGE_PERCENT_EACH_SIDE,
        "brokerage_per_order": _brokerage(broker_name),
    }


def _instrument_of(trade, fallback):
    return str(
        trade.get("instrument")
        or trade.get("underlying")
        or fallback
        or "NIFTY"
    ).upper()


def _apply_costs_to_result(result, broker_name, fallback_instrument=None):
    if not isinstance(result, dict) or not result.get("success"):
        return result

    output = deepcopy(result)
    trades = []
    totals = {
        "market_gross_pnl": 0.0,
        "slippage_cost": 0.0,
        "brokerage": 0.0,
        "transaction_charges": 0.0,
        "stt": 0.0,
        "stamp_duty": 0.0,
        "sebi_charges": 0.0,
        "ipft": 0.0,
        "gst": 0.0,
        "total_charges": 0.0,
        "net_pnl": 0.0,
    }

    for original in output.get("trades", []) or []:
        trade = dict(original)
        instrument = _instrument_of(trade, fallback_instrument or output.get("instrument"))
        qty = max(1, _i(trade.get("qty"), 1))
        costs = calculate_option_round_trip_costs(
            broker_name,
            instrument,
            trade.get("entry_price"),
            trade.get("exit_price"),
            qty,
        )
        trade["instrument"] = instrument
        trade["gross_pnl"] = costs["market_gross_pnl"]
        trade["slippage_cost"] = costs["slippage_cost"]
        trade["charges"] = costs
        trade["total_charges"] = costs["total_charges"]
        trade["pnl"] = costs["net_pnl"]
        trade["pnl_after_all_costs"] = costs["net_pnl"]
        trade["cost_model"] = "INDIA_INDEX_OPTIONS_ALL_COSTS_V2"
        trades.append(trade)

        for key in totals:
            totals[key] += _f(costs.get(key), 0)

    for key in totals:
        totals[key] = round(totals[key], 2)

    wins = sum(1 for trade in trades if _f(trade.get("pnl")) > 0)
    losses = sum(1 for trade in trades if _f(trade.get("pnl")) < 0)
    flat = len(trades) - wins - losses
    net_pnl = totals["net_pnl"]
    capital = _f(output.get("capital", (output.get("summary") or {}).get("capital", 0)), 0)

    output.update({
        "trades": trades,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "flat_trades": flat,
        "win_rate": round(wins / len(trades) * 100.0, 2) if trades else 0.0,
        "gross_pnl_before_costs": totals["market_gross_pnl"],
        "total_slippage_cost": totals["slippage_cost"],
        "total_brokerage": totals["brokerage"],
        "total_statutory_charges": round(
            totals["transaction_charges"]
            + totals["stt"]
            + totals["stamp_duty"]
            + totals["sebi_charges"]
            + totals["ipft"]
            + totals["gst"],
            2,
        ),
        "total_charges": totals["total_charges"],
        "total_pnl": net_pnl,
        "net_pnl": net_pnl,
        "ending_capital": round(capital + net_pnl, 2),
        "costs_applied": True,
        "cost_model": "INDIA_INDEX_OPTIONS_ALL_COSTS_V2",
        "execution_model": {
            "entry_fill": "one_tick_or_0.10pct_adverse_whichever_higher",
            "exit_fill": "one_tick_or_0.10pct_adverse_whichever_higher",
            "brokerage": "broker_specific_flat_per_executed_order",
            "includes": [
                "brokerage", "exchange_transaction", "STT", "stamp_duty",
                "SEBI", "IPFT_when_NSE", "GST", "slippage",
            ],
        },
        "premium_accuracy": {
            "signal_candles": "REAL_ONE_MINUTE_INDEX_OHLC",
            "option_premium": "ATR_SYNTHETIC_ESTIMATE",
            "accuracy_grade": "ESTIMATED_NOT_EXACT_LIVE",
            "exact_live_match_requires": "HISTORICAL_EXPIRED_OPTION_OHLC_FOR_SELECTED_STRIKE",
        },
    })

    summary = dict(output.get("summary") or {})
    summary.update({
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": output["win_rate"],
        "capital": capital,
        "gross_pnl": totals["market_gross_pnl"],
        "slippage_cost": totals["slippage_cost"],
        "charges": totals["total_charges"],
        "net_pnl": net_pnl,
        "ending_capital": output["ending_capital"],
        "pnl_basis": "AFTER_SLIPPAGE_BROKERAGE_AND_ALL_STATUTORY_CHARGES",
    })
    output["summary"] = summary
    return output


def apply_backtest_realism_costs_patch():
    if getattr(routes, "_okai_realism_costs_v2", False):
        return

    original_single = routes.run_realistic_day_backtest
    original_auto = routes._okai_run_auto_index_backtest

    def single_with_costs(
        broker_name, obj, instrument, date_str, capital,
        entry_threshold, sl_percent, target_percent,
    ):
        result = original_single(
            broker_name, obj, instrument, date_str, capital,
            entry_threshold, sl_percent, target_percent,
        )
        return _apply_costs_to_result(result, broker_name, instrument)

    def auto_with_costs(
        broker_name, obj, date_str, capital,
        entry_threshold, sl_percent, target_percent,
    ):
        result = original_auto(
            broker_name, obj, date_str, capital,
            entry_threshold, sl_percent, target_percent,
        )
        return _apply_costs_to_result(result, broker_name, "AUTO")

    routes.run_realistic_day_backtest = single_with_costs
    routes._okai_run_auto_index_backtest = auto_with_costs
    routes.calculate_option_round_trip_costs = calculate_option_round_trip_costs
    routes._okai_realism_costs_v2 = True
