"""Cost-safe break-even and hard per-trade risk controls for backtests.

True break-even is not the raw entry premium.  It is the exit price that still
leaves 2% net profit on entry turnover after conservative slippage, brokerage
and all statutory option charges.

The 90% portfolio setting remains a maximum allocation.  Position quantity is
reduced when needed so the initial stop risk never exceeds the smaller of:
  * 1% of equity before the trade, and
  * 8% of option premium per unit.
"""

from copy import deepcopy
from contextlib import contextmanager
from functools import lru_cache
import math
import threading

from backtest import routes
from backtest.realism_costs_patch import calculate_option_round_trip_costs


TICK_SIZE = 0.05
NET_PROFIT_LOCK_PERCENT = 2.0
MAX_EQUITY_RISK_PERCENT = 1.0
MAX_PREMIUM_RISK_PERCENT = 8.0

_context = threading.local()


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


def _round_up_tick(value):
    ticks = math.ceil(max(TICK_SIZE, _f(value, TICK_SIZE)) / TICK_SIZE - 1e-12)
    return round(ticks * TICK_SIZE, 2)


@lru_cache(maxsize=4096)
def calculate_cost_safe_breakeven_price(
    broker_name,
    instrument,
    entry_price,
    quantity,
    net_profit_percent=NET_PROFIT_LOCK_PERCENT,
):
    """Return the minimum raw exit price giving costs + requested net profit."""
    entry = max(TICK_SIZE, _f(entry_price, TICK_SIZE))
    qty = max(1, _i(quantity, 1))
    target_net = entry * qty * max(0.0, _f(net_profit_percent)) / 100.0

    low = entry
    high = max(entry + TICK_SIZE, entry * 1.10)

    def net_at(exit_price):
        costs = calculate_option_round_trip_costs(
            broker_name,
            instrument,
            entry,
            exit_price,
            qty,
        )
        return _f(costs.get("net_pnl"), -1e18)

    for _ in range(20):
        if net_at(high) >= target_net:
            break
        high = high * 1.25 + TICK_SIZE
    else:
        raise RuntimeError("Unable to solve cost-safe break-even price")

    for _ in range(60):
        middle = (low + high) / 2.0
        if net_at(middle) >= target_net:
            high = middle
        else:
            low = middle

    solved = _round_up_tick(high)
    for _ in range(20):
        if net_at(solved) >= target_net:
            break
        solved = round(solved + TICK_SIZE, 2)

    final_costs = calculate_option_round_trip_costs(
        broker_name,
        instrument,
        entry,
        solved,
        qty,
    )
    return {
        "price": solved,
        "target_net_profit": round(target_net, 2),
        "net_pnl_at_price": round(_f(final_costs.get("net_pnl")), 2),
        "total_charges_at_price": round(_f(final_costs.get("total_charges")), 2),
        "slippage_cost_at_price": round(_f(final_costs.get("slippage_cost")), 2),
        "net_profit_percent": round(max(0.0, _f(net_profit_percent)), 2),
        "quantity_basis": qty,
        "instrument_basis": str(instrument or "NIFTY").upper(),
        "broker_basis": str(broker_name or "angelone").lower(),
    }


@contextmanager
def _trail_context(broker_name, instrument, quantity):
    previous = getattr(_context, "value", None)
    _context.value = {
        "broker_name": str(broker_name or "angelone").lower(),
        "instrument": str(instrument or "NIFTY").upper(),
        "quantity": max(1, _i(quantity, 1)),
    }
    try:
        yield
    finally:
        _context.value = previous


def _context_value():
    value = getattr(_context, "value", None)
    if isinstance(value, dict):
        return value
    # AUTO fallback is deliberately conservative: smallest configured lot and
    # NSE cost rate.  Per-instrument wrappers replace this whenever available.
    return {
        "broker_name": "angelone",
        "instrument": "NIFTY",
        "quantity": min(routes.LOT_SIZES.values()) if routes.LOT_SIZES else 1,
    }


def _cost_safe_profit_lock(
    entry_price,
    initial_risk,
    current_sl,
    peak_price,
    current_price,
):
    entry = max(TICK_SIZE, _f(entry_price, TICK_SIZE))
    risk = max(TICK_SIZE, _f(initial_risk, TICK_SIZE))
    old_sl = max(TICK_SIZE, _f(current_sl, entry - risk))
    current = max(TICK_SIZE, _f(current_price, TICK_SIZE))
    peak = max(entry, _f(peak_price, entry), current)
    peak_r = (peak - entry) / risk

    ctx = _context_value()
    true_be = calculate_cost_safe_breakeven_price(
        ctx["broker_name"],
        ctx["instrument"],
        entry,
        ctx["quantity"],
        NET_PROFIT_LOCK_PERCENT,
    )
    be_price = _f(true_be["price"], entry)

    new_sl = old_sl
    stage = "INITIAL_ATR"
    locked_r = -1.0
    be_triggered = peak + 1e-9 >= be_price + TICK_SIZE

    # First trail only after charges and 2% net profit are both covered.
    if be_triggered:
        new_sl = max(new_sl, be_price)
        stage = "COST_PLUS_2PCT_BREAKEVEN"
        locked_r = (new_sl - entry) / risk

        if peak_r >= 1.20:
            new_sl = max(new_sl, entry + 0.50 * risk)
            stage = "LOCK_0_5R_AFTER_TRUE_BE"
            locked_r = (new_sl - entry) / risk

        if peak_r >= 1.80:
            new_sl = max(
                new_sl,
                entry + risk,
                peak - 0.80 * risk,
            )
            stage = "DYNAMIC_PROFIT_LOCK_AFTER_TRUE_BE"
            locked_r = (new_sl - entry) / risk

    # Keep one tick below the observed peak, but never call it true BE unless
    # the cost-safe price itself can be retained.
    peak_room = max(TICK_SIZE, peak - TICK_SIZE)
    candidate = min(new_sl, peak_room)
    if be_triggered and candidate + 1e-9 < be_price:
        candidate = old_sl
        stage = "WAITING_TRUE_BE_PRICE_ROOM"
        locked_r = (candidate - entry) / risk
        be_triggered = False
    new_sl = candidate

    return {
        "sl_price": round(new_sl, 2),
        "old_sl_price": round(old_sl, 2),
        "updated": new_sl > old_sl + 1e-9,
        "peak_price": round(peak, 2),
        "peak_r": round(peak_r, 2),
        "locked_r": round(locked_r, 2),
        "stage": stage,
        "initial_risk": round(risk, 2),
        "cost_safe_breakeven_price": round(be_price, 2),
        "breakeven_triggered": bool(be_triggered),
        "breakeven_rule": "ENTRY_PLUS_ALL_COSTS_PLUS_2PCT_NET",
        "breakeven_target_net_profit": true_be["target_net_profit"],
        "breakeven_net_pnl_at_stop": true_be["net_pnl_at_price"],
        "breakeven_total_charges": true_be["total_charges_at_price"],
    }


def _is_hero_trade(trade, result):
    fields = [
        trade.get("mode"),
        trade.get("strategy_mode"),
        result.get("strategy_mode"),
    ]
    return any("HERO" in str(value or "").upper() for value in fields)


def _risk_capped_trade(trade, result, broker_name, equity):
    output = dict(trade)
    instrument = str(
        output.get("instrument")
        or output.get("underlying")
        or result.get("instrument")
        or "NIFTY"
    ).upper()

    if instrument == "AUTO":
        instrument = str(output.get("selected_instrument") or "NIFTY").upper()

    lot_size = max(1, _i(output.get("lot_size"), routes.LOT_SIZES.get(instrument, 1)))
    entry = max(TICK_SIZE, _f(output.get("entry_price"), TICK_SIZE))
    original_exit = max(TICK_SIZE, _f(output.get("exit_price"), TICK_SIZE))
    original_qty = max(lot_size, _i(output.get("qty"), lot_size))
    original_lots = max(1, original_qty // lot_size)

    atr_risk = max(TICK_SIZE, _f(output.get("risk_points"), entry * 0.08))
    premium_cap_risk = max(TICK_SIZE, entry * MAX_PREMIUM_RISK_PERCENT / 100.0)
    risk_points = min(atr_risk, premium_cap_risk)
    max_loss_rupees = max(0.0, _f(equity) * MAX_EQUITY_RISK_PERCENT / 100.0)
    risk_per_lot = risk_points * lot_size
    allowed_lots = int(max_loss_rupees // risk_per_lot) if risk_per_lot > 0 else 0
    allowed_lots = min(original_lots, allowed_lots)

    if allowed_lots < 1:
        output.update({
            "risk_cap_skipped": True,
            "risk_cap_skip_reason": "ONE_LOT_RISK_EXCEEDS_1PCT_EQUITY",
            "qty_before_risk_cap": original_qty,
            "lots_before_risk_cap": original_lots,
            "qty": 0,
            "lots": 0,
            "pnl": 0.0,
            "max_loss_rupees": round(max_loss_rupees, 2),
            "risk_points_before_cap": round(atr_risk, 2),
            "risk_points_after_cap": round(risk_points, 2),
        })
        return output

    qty = allowed_lots * lot_size
    hard_sl = round(max(TICK_SIZE, entry - risk_points), 2)
    reason = str(output.get("reason") or "").upper()
    adjusted_exit = original_exit

    # Old ATR exits can be far below the 8%-premium / 1%-equity hard stop.
    if "PURE_ATR_SL" in reason and original_exit < hard_sl:
        adjusted_exit = hard_sl
        output["reason"] = "HARD_RISK_CAP_SL"
        output["original_exit_price_before_risk_cap"] = round(original_exit, 2)

    true_be = calculate_cost_safe_breakeven_price(
        broker_name,
        instrument,
        entry,
        qty,
        NET_PROFIT_LOCK_PERCENT,
    )

    # Defensive correction for old/raw-entry PROFIT_LOCK rows.
    if (
        "PROFIT_LOCK_TRAIL" in reason
        and adjusted_exit + 1e-9 < _f(true_be["price"])
        and _f(output.get("peak_price"), entry) + 1e-9 >= _f(true_be["price"])
    ):
        adjusted_exit = _f(true_be["price"])
        output["reason"] = "TRUE_BE_PLUS_2PCT_TRAIL"

    costs = calculate_option_round_trip_costs(
        broker_name,
        instrument,
        entry,
        adjusted_exit,
        qty,
    )

    output.update({
        "instrument": instrument,
        "lot_size": lot_size,
        "qty_before_risk_cap": original_qty,
        "lots_before_risk_cap": original_lots,
        "qty": qty,
        "lots": allowed_lots,
        "capital_used": round(entry * qty, 2),
        "used_capital": round(entry * qty, 2),
        "entry_price": round(entry, 2),
        "exit_price": round(adjusted_exit, 2),
        "initial_sl_price": round(hard_sl, 2),
        "sl_price": round(max(hard_sl, _f(output.get("sl_price"), hard_sl)), 2),
        "risk_points_before_cap": round(atr_risk, 2),
        "risk_points": round(risk_points, 2),
        "risk_points_after_cap": round(risk_points, 2),
        "premium_risk_cap_percent": MAX_PREMIUM_RISK_PERCENT,
        "equity_risk_cap_percent": MAX_EQUITY_RISK_PERCENT,
        "max_loss_rupees": round(max_loss_rupees, 2),
        "risk_per_lot_rupees": round(risk_per_lot, 2),
        "risk_cap_applied": bool(
            qty < original_qty
            or risk_points + 1e-9 < atr_risk
            or adjusted_exit != original_exit
        ),
        "cost_safe_breakeven_price": true_be["price"],
        "breakeven_rule": "ENTRY_PLUS_ALL_COSTS_PLUS_2PCT_NET",
        "breakeven_target_net_profit": true_be["target_net_profit"],
        "breakeven_net_pnl_at_stop": true_be["net_pnl_at_price"],
        "breakeven_total_charges": true_be["total_charges_at_price"],
        "gross_pnl": costs["market_gross_pnl"],
        "slippage_cost": costs["slippage_cost"],
        "charges": costs,
        "total_charges": costs["total_charges"],
        "pnl": costs["net_pnl"],
        "pnl_after_all_costs": costs["net_pnl"],
        "cost_model": "INDIA_INDEX_OPTIONS_ALL_COSTS_V2",
    })
    return output


def _recalculate_result(result, broker_name):
    if not isinstance(result, dict) or not result.get("success"):
        return result

    output = deepcopy(result)
    trades = []
    equity = _f(output.get("capital"), _f((output.get("summary") or {}).get("capital"), 0))
    peak_equity = equity
    max_drawdown = 0.0
    total_charges = 0.0
    total_slippage = 0.0
    gross_pnl = 0.0

    for original in output.get("trades", []) or []:
        if _is_hero_trade(original, output):
            trade = dict(original)
        else:
            trade = _risk_capped_trade(original, output, broker_name, equity)
            if trade.get("risk_cap_skipped"):
                continue

        pnl = _f(trade.get("pnl"))
        trades.append(trade)
        equity += pnl
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, peak_equity - equity)
        total_charges += _f(trade.get("total_charges"))
        total_slippage += _f(trade.get("slippage_cost"))
        fallback_gross = (
            _f(trade.get("exit_price"))
            - _f(trade.get("entry_price"))
        ) * _i(trade.get("qty"))
        gross_pnl += _f(trade.get("gross_pnl"), fallback_gross)

    wins = sum(1 for trade in trades if _f(trade.get("pnl")) > 0)
    losses = sum(1 for trade in trades if _f(trade.get("pnl")) < 0)
    flats = len(trades) - wins - losses
    starting_capital = _f(output.get("capital"), _f((output.get("summary") or {}).get("capital"), 0))
    total_pnl = round(sum(_f(trade.get("pnl")) for trade in trades), 2)
    ending_capital = round(starting_capital + total_pnl, 2)

    output.update({
        "trades": trades,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "flat_trades": flats,
        "win_rate": round(wins / len(trades) * 100.0, 2) if trades else 0.0,
        "total_pnl": total_pnl,
        "net_pnl": total_pnl,
        "ending_capital": ending_capital,
        "gross_pnl_before_costs": round(gross_pnl, 2),
        "total_charges": round(total_charges, 2),
        "total_slippage_cost": round(total_slippage, 2),
        "max_drawdown": round(max_drawdown, 2),
        "true_breakeven_enabled": True,
        "true_breakeven_rule": "ENTRY_PLUS_ALL_COSTS_PLUS_2PCT_NET",
        "risk_cap_enabled": True,
        "risk_cap_rule": "MIN_1PCT_EQUITY_OR_8PCT_PREMIUM",
        "capital_use_rule": "90PCT_MAXIMUM_NOT_FORCED",
        "cost_safe_be_risk_patch": "V1",
        "costs_applied": True,
    })

    summary = dict(output.get("summary") or {})
    summary.update({
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "flat_trades": flats,
        "win_rate": output["win_rate"],
        "capital": starting_capital,
        "net_pnl": total_pnl,
        "ending_capital": ending_capital,
        "max_drawdown": round(max_drawdown, 2),
        "true_breakeven_enabled": True,
        "true_breakeven_rule": "ENTRY_PLUS_ALL_COSTS_PLUS_2PCT_NET",
        "risk_cap_enabled": True,
        "risk_cap_rule": "MIN_1PCT_EQUITY_OR_8PCT_PREMIUM",
        "capital_use_percent": 90,
        "capital_use_rule": "MAXIMUM_NOT_FORCED",
    })
    output["summary"] = summary
    return output


def apply_cost_safe_breakeven_risk_patch():
    if getattr(routes, "_okai_cost_safe_be_risk_v1", False):
        return

    routes.update_option_profit_lock = _cost_safe_profit_lock
    routes.calculate_cost_safe_breakeven_price = calculate_cost_safe_breakeven_price

    # The AUTO portfolio keeps a captured one-index function.  Wrapping it gives
    # the trail engine the exact broker, instrument and one-lot quantity.
    captured_single = getattr(routes, "_OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST", None)
    if callable(captured_single):
        def captured_with_context(
            broker_name,
            obj,
            instrument,
            date_str,
            capital,
            entry_threshold,
            sl_percent,
            target_percent,
        ):
            qty = routes.LOT_SIZES.get(str(instrument).upper(), 1)
            with _trail_context(broker_name, instrument, qty):
                return captured_single(
                    broker_name,
                    obj,
                    instrument,
                    date_str,
                    capital,
                    entry_threshold,
                    sl_percent,
                    target_percent,
                )

        routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST = captured_with_context

    original_single = routes.run_realistic_day_backtest

    def single_with_true_be_and_risk(
        broker_name,
        obj,
        instrument,
        date_str,
        capital,
        entry_threshold,
        sl_percent,
        target_percent,
    ):
        qty = routes.LOT_SIZES.get(str(instrument).upper(), 1)
        with _trail_context(broker_name, instrument, qty):
            result = original_single(
                broker_name,
                obj,
                instrument,
                date_str,
                capital,
                entry_threshold,
                sl_percent,
                target_percent,
            )
        return _recalculate_result(result, broker_name)

    original_auto = routes._okai_run_auto_index_backtest

    def auto_with_true_be_and_risk(
        broker_name,
        obj,
        date_str,
        capital,
        entry_threshold,
        sl_percent,
        target_percent,
    ):
        conservative_qty = min(routes.LOT_SIZES.values()) if routes.LOT_SIZES else 1
        with _trail_context(broker_name, "NIFTY", conservative_qty):
            result = original_auto(
                broker_name,
                obj,
                date_str,
                capital,
                entry_threshold,
                sl_percent,
                target_percent,
            )
        return _recalculate_result(result, broker_name)

    routes.run_realistic_day_backtest = single_with_true_be_and_risk
    routes._okai_run_auto_index_backtest = auto_with_true_be_and_risk
    routes._okai_cost_safe_be_risk_v1 = True
