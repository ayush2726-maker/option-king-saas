"""Exact Paper/Live cost-safe break-even and net P&L.

Paper and Live use the same strategy, but their execution accounting differs:

* PAPER uses the observed option LTP plus conservative simulated slippage and
  estimated brokerage/statutory charges.
* LIVE uses actual broker average fill prices, so slippage is already present
  in those fills; only estimated brokerage/statutory charges are deducted.

The first profit-lock trail is solved for the actual broker, index and trade
quantity.  It activates only at entry + round-trip costs + 2% net profit.
"""

from contextlib import contextmanager
from functools import lru_cache
import json
import math
import threading

from backtest import realism_costs_patch as cost_model
from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime


TICK_SIZE = 0.05
NET_PROFIT_LOCK_PERCENT = 2.0
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


def calculate_execution_costs(
    broker_name,
    instrument,
    entry_price,
    exit_price,
    quantity,
    include_slippage,
):
    """Return net P&L with the correct Paper/Live execution basis."""
    broker = str(broker_name or "angelone").lower()
    index = str(instrument or "NIFTY").upper()
    entry = max(TICK_SIZE, _f(entry_price, TICK_SIZE))
    exit_value = max(TICK_SIZE, _f(exit_price, TICK_SIZE))
    qty = max(1, _i(quantity, 1))

    if include_slippage:
        result = dict(
            cost_model.calculate_option_round_trip_costs(
                broker,
                index,
                entry,
                exit_value,
                qty,
            )
        )
        result["execution_basis"] = (
            "PAPER_LTP_WITH_ESTIMATED_SLIPPAGE_AND_CHARGES"
        )
        return result

    # Live entry/exit are broker average fills, therefore do not model another
    # layer of slippage on top of those actual fills.
    buy_turnover = entry * qty
    sell_turnover = exit_value * qty
    total_turnover = buy_turnover + sell_turnover
    brokerage = cost_model._brokerage(broker) * 2.0
    is_bse = index == "SENSEX"
    transaction_rate = (
        cost_model.BSE_SENSEX_OPTION_TRANSACTION_PERCENT
        if is_bse
        else cost_model.NSE_OPTION_TRANSACTION_PERCENT
    )
    transaction = total_turnover * transaction_rate / 100.0
    stt = sell_turnover * cost_model.OPTION_STT_SELL_PERCENT / 100.0
    stamp = buy_turnover * cost_model.OPTION_STAMP_BUY_PERCENT / 100.0
    sebi = total_turnover * cost_model.SEBI_PERCENT / 100.0
    ipft = (
        0.0
        if is_bse
        else total_turnover * cost_model.NSE_IPFT_PERCENT / 100.0
    )
    gst = (
        brokerage + transaction + sebi + ipft
    ) * cost_model.GST_PERCENT / 100.0
    total_charges = brokerage + transaction + stt + stamp + sebi + ipft + gst
    gross = (exit_value - entry) * qty
    net = gross - total_charges

    return {
        "simulated_entry_price": round(entry, 2),
        "simulated_exit_price": round(exit_value, 2),
        "buy_turnover": round(buy_turnover, 2),
        "sell_turnover": round(sell_turnover, 2),
        "total_turnover": round(total_turnover, 2),
        "market_gross_pnl": round(gross, 2),
        "slippage_cost": 0.0,
        "gross_pnl_after_slippage": round(gross, 2),
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
        "stt_sell_percent": cost_model.OPTION_STT_SELL_PERCENT,
        "stamp_buy_percent": cost_model.OPTION_STAMP_BUY_PERCENT,
        "slippage_percent_each_side": 0.0,
        "brokerage_per_order": cost_model._brokerage(broker),
        "execution_basis": "LIVE_ACTUAL_FILLS_MINUS_ESTIMATED_CHARGES",
    }


@lru_cache(maxsize=4096)
def calculate_exact_breakeven_price(
    broker_name,
    instrument,
    entry_price,
    quantity,
    mode,
    net_profit_percent=NET_PROFIT_LOCK_PERCENT,
):
    entry = max(TICK_SIZE, _f(entry_price, TICK_SIZE))
    qty = max(1, _i(quantity, 1))
    trading_mode = "live" if str(mode).lower() == "live" else "paper"
    include_slippage = trading_mode == "paper"
    target_net = entry * qty * max(0.0, _f(net_profit_percent)) / 100.0

    def net_at(exit_value):
        return _f(
            calculate_execution_costs(
                broker_name,
                instrument,
                entry,
                exit_value,
                qty,
                include_slippage,
            ).get("net_pnl"),
            -1e18,
        )

    low = entry
    high = max(entry + TICK_SIZE, entry * 1.10)
    for _ in range(20):
        if net_at(high) >= target_net:
            break
        high = high * 1.25 + TICK_SIZE
    else:
        raise RuntimeError("Unable to solve exact Paper/Live break-even")

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

    final = calculate_execution_costs(
        broker_name,
        instrument,
        entry,
        solved,
        qty,
        include_slippage,
    )
    return {
        "price": solved,
        "target_net_profit": round(target_net, 2),
        "net_pnl_at_price": round(_f(final.get("net_pnl")), 2),
        "total_charges_at_price": round(_f(final.get("total_charges")), 2),
        "slippage_cost_at_price": round(_f(final.get("slippage_cost")), 2),
        "net_profit_percent": round(_f(net_profit_percent), 2),
        "quantity_basis": qty,
        "instrument_basis": str(instrument or "NIFTY").upper(),
        "broker_basis": str(broker_name or "angelone").lower(),
        "trading_mode_basis": trading_mode,
        "execution_basis": final.get("execution_basis"),
    }


@contextmanager
def _trade_context(trade):
    previous = getattr(_context, "value", None)
    _context.value = {
        "broker_name": str(runtime._v(trade, "broker_name", "angelone") or "angelone").lower(),
        "instrument": runtime._underlying(trade),
        "quantity": max(1, _i(runtime._v(trade, "qty", 1), 1)),
        "mode": runtime._mode(trade),
    }
    try:
        yield
    finally:
        _context.value = previous


def _current_context():
    value = getattr(_context, "value", None)
    if isinstance(value, dict):
        return value
    return {
        "broker_name": "angelone",
        "instrument": "NIFTY",
        "quantity": 1,
        "mode": "paper",
    }


def _exact_profit_lock(
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

    ctx = _current_context()
    true_be = calculate_exact_breakeven_price(
        ctx["broker_name"],
        ctx["instrument"],
        round(entry, 4),
        ctx["quantity"],
        ctx["mode"],
        NET_PROFIT_LOCK_PERCENT,
    )
    be_price = _f(true_be["price"], entry)

    new_sl = old_sl
    stage = "INITIAL_ATR"
    locked_r = -1.0
    triggered = peak + 1e-9 >= be_price + TICK_SIZE

    if triggered:
        new_sl = max(new_sl, be_price)
        stage = "EXACT_COST_PLUS_2PCT_BREAKEVEN"
        locked_r = (new_sl - entry) / risk

        if peak_r >= 1.20:
            new_sl = max(new_sl, entry + 0.50 * risk)
            stage = "LOCK_0_5R_AFTER_EXACT_TRUE_BE"
            locked_r = (new_sl - entry) / risk

        if peak_r >= 1.80:
            new_sl = max(new_sl, entry + risk, peak - 0.80 * risk)
            stage = "DYNAMIC_PROFIT_LOCK_AFTER_EXACT_TRUE_BE"
            locked_r = (new_sl - entry) / risk

    peak_room = max(TICK_SIZE, peak - TICK_SIZE)
    candidate = min(new_sl, peak_room)
    if triggered and candidate + 1e-9 < be_price:
        candidate = old_sl
        stage = "WAITING_EXACT_TRUE_BE_PRICE_ROOM"
        locked_r = (candidate - entry) / risk
        triggered = False

    return {
        "sl_price": round(candidate, 2),
        "old_sl_price": round(old_sl, 2),
        "updated": candidate > old_sl + 1e-9,
        "peak_price": round(peak, 2),
        "peak_r": round(peak_r, 2),
        "locked_r": round(locked_r, 2),
        "stage": stage,
        "initial_risk": round(risk, 2),
        "cost_safe_breakeven_price": round(be_price, 2),
        "breakeven_triggered": bool(triggered),
        "breakeven_rule": "ENTRY_PLUS_EXACT_TRADE_COSTS_PLUS_2PCT_NET",
        "breakeven_target_net_profit": true_be["target_net_profit"],
        "breakeven_net_pnl_at_stop": true_be["net_pnl_at_price"],
        "breakeven_total_charges": true_be["total_charges_at_price"],
        "breakeven_slippage_cost": true_be["slippage_cost_at_price"],
        "breakeven_quantity_basis": true_be["quantity_basis"],
        "breakeven_instrument_basis": true_be["instrument_basis"],
        "breakeven_broker_basis": true_be["broker_basis"],
        "breakeven_trading_mode_basis": true_be["trading_mode_basis"],
    }


def apply_live_net_pnl_breakeven_patch():
    if getattr(runtime, "_okai_live_net_pnl_exact_be_v1", False):
        return

    original_ensure_schema = runtime._ensure_schema
    original_evaluate_exit = runtime._evaluate_exit
    original_update_open = runtime._update_open

    def ensure_schema_with_costs(conn):
        original_ensure_schema(conn)
        for name, kind in [
            ("gross_pnl", "REAL"),
            ("slippage_cost", "REAL"),
            ("total_charges", "REAL"),
            ("brokerage", "REAL"),
            ("statutory_charges", "REAL"),
            ("net_pnl", "REAL"),
            ("pnl_basis", "TEXT"),
            ("charges_json", "TEXT"),
            ("cost_safe_breakeven_price", "REAL"),
            ("breakeven_rule", "TEXT"),
            ("breakeven_total_charges", "REAL"),
            ("breakeven_target_net_profit", "REAL"),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}"
                )
            except Exception:
                pass
        conn.commit()

    def evaluate_exit_with_context(trade, ltp, market_data, candle_id):
        with _trade_context(trade):
            return original_evaluate_exit(trade, ltp, market_data, candle_id)

    def update_open_with_breakeven(conn, trade, ltp, evaluation):
        original_update_open(conn, trade, ltp, evaluation)
        trail = evaluation.get("trail") or {}
        try:
            conn.execute(
                """
                UPDATE paper_trades
                SET cost_safe_breakeven_price=?,
                    breakeven_rule=?,
                    breakeven_total_charges=?,
                    breakeven_target_net_profit=?
                WHERE id=?
                """,
                (
                    trail.get("cost_safe_breakeven_price"),
                    trail.get("breakeven_rule"),
                    trail.get("breakeven_total_charges"),
                    trail.get("breakeven_target_net_profit"),
                    trade["id"],
                ),
            )
            conn.commit()
        except Exception:
            pass

    def close_with_net_costs(
        conn,
        user_id,
        trade,
        price,
        reason,
        order_id=None,
    ):
        qty = max(1, _i(runtime._v(trade, "qty", 1), 1))
        entry = _f(runtime._v(trade, "entry_price", 0), 0)
        mode = runtime._mode(trade)
        broker = str(runtime._v(trade, "broker_name", "angelone") or "angelone")
        instrument = runtime._underlying(trade)
        costs = calculate_execution_costs(
            broker,
            instrument,
            entry,
            price,
            qty,
            include_slippage=(mode == "paper"),
        )
        gross = _f(costs.get("market_gross_pnl"), 0)
        net = _f(costs.get("net_pnl"), 0)
        total_charges = _f(costs.get("total_charges"), 0)
        brokerage = _f(costs.get("brokerage"), 0)
        statutory = max(0.0, total_charges - brokerage)
        pnl_basis = str(costs.get("execution_basis") or "NET_AFTER_COSTS")

        conn.execute(
            """
            UPDATE paper_trades
            SET exit_price=?,
                pnl=?,
                gross_pnl=?,
                slippage_cost=?,
                total_charges=?,
                brokerage=?,
                statutory_charges=?,
                net_pnl=?,
                pnl_basis=?,
                charges_json=?,
                status='CLOSED',
                reason=?,
                last_ltp=?,
                exit_order_id=?,
                live_order_status=?
            WHERE id=?
            """,
            (
                price,
                round(net, 2),
                round(gross, 2),
                round(_f(costs.get("slippage_cost"), 0), 2),
                round(total_charges, 2),
                round(brokerage, 2),
                round(statutory, 2),
                round(net, 2),
                pnl_basis,
                json.dumps(costs, separators=(",", ":"), sort_keys=True),
                reason,
                price,
                order_id,
                "EXIT_FILLED" if mode == "live" else "PAPER_CLOSED",
                trade["id"],
            ),
        )
        conn.commit()

        try:
            runtime.notify_user(
                user_id,
                "\n".join(
                    [
                        "📤 <b>Portfolio Exit</b>",
                        f"Mode: {mode.upper()}",
                        f"Index: {instrument}",
                        f"Symbol: {trade['symbol']}",
                        f"Qty: {qty}",
                        f"Exit: ₹{price:.2f}",
                        f"Gross P&L: ₹{gross:.2f}",
                        f"Charges/Slippage: ₹{(gross-net):.2f}",
                        f"Net P&L: ₹{net:.2f}",
                        f"Reason: {reason}",
                    ]
                ),
            )
        except Exception:
            pass

    runtime._ensure_schema = ensure_schema_with_costs
    runtime._evaluate_exit = evaluate_exit_with_context
    runtime._update_open = update_open_with_breakeven
    runtime._close = close_with_net_costs

    # Structural Exit V4 resolves this helper dynamically through angel_fetcher.
    angel_fetcher._dynamic_profit_lock = _exact_profit_lock

    runtime._okai_live_net_pnl_exact_be_v1 = True
