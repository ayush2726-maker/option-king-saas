import math


TICK_SIZE = 0.05
MAX_PREMIUM_RISK_PERCENT = 8.0
TRUE_BE_NET_PROFIT_PERCENT = 2.0

# Conservative basis used because this shared exit helper does not receive the
# broker/instrument/quantity. Smallest supported index lot makes per-unit flat
# brokerage highest, and the NSE rate is slightly higher than the BSE rate.
_CONSERVATIVE_QTY = 20
_BROKERAGE_PER_ORDER = 20.0
_TRANSACTION_PERCENT = 0.03553
_STT_SELL_PERCENT = 0.15
_STAMP_BUY_PERCENT = 0.003
_SEBI_PERCENT = 0.0001
_IPFT_PERCENT = 0.0000001
_GST_PERCENT = 18.0
_SLIPPAGE_PERCENT_EACH_SIDE = 0.10


def _round_up_tick(value):
    ticks = math.ceil(max(TICK_SIZE, float(value or TICK_SIZE)) / TICK_SIZE - 1e-12)
    return round(ticks * TICK_SIZE, 2)


def _costs_and_net(entry_price, exit_price, quantity=_CONSERVATIVE_QTY):
    entry = max(TICK_SIZE, float(entry_price or TICK_SIZE))
    exit_value = max(TICK_SIZE, float(exit_price or TICK_SIZE))
    qty = max(1, int(quantity or 1))

    entry_slip = max(TICK_SIZE, entry * _SLIPPAGE_PERCENT_EACH_SIDE / 100.0)
    exit_slip = max(TICK_SIZE, exit_value * _SLIPPAGE_PERCENT_EACH_SIDE / 100.0)
    simulated_entry = round(entry + entry_slip, 2)
    simulated_exit = round(max(TICK_SIZE, exit_value - exit_slip), 2)

    buy_turnover = simulated_entry * qty
    sell_turnover = simulated_exit * qty
    total_turnover = buy_turnover + sell_turnover
    brokerage = _BROKERAGE_PER_ORDER * 2.0
    transaction = total_turnover * _TRANSACTION_PERCENT / 100.0
    stt = sell_turnover * _STT_SELL_PERCENT / 100.0
    stamp = buy_turnover * _STAMP_BUY_PERCENT / 100.0
    sebi = total_turnover * _SEBI_PERCENT / 100.0
    ipft = total_turnover * _IPFT_PERCENT / 100.0
    gst = (brokerage + transaction + sebi + ipft) * _GST_PERCENT / 100.0
    total_charges = brokerage + transaction + stt + stamp + sebi + ipft + gst
    net_pnl = (simulated_exit - simulated_entry) * qty - total_charges

    return {
        "net_pnl": round(net_pnl, 2),
        "total_charges": round(total_charges, 2),
        "simulated_entry_price": round(simulated_entry, 2),
        "simulated_exit_price": round(simulated_exit, 2),
    }


def calculate_cost_safe_breakeven_price(
    entry_price,
    net_profit_percent=TRUE_BE_NET_PROFIT_PERCENT,
):
    """Conservative exit price covering all costs plus requested net profit."""
    entry = max(TICK_SIZE, float(entry_price or TICK_SIZE))
    target_net = entry * _CONSERVATIVE_QTY * max(
        0.0,
        float(net_profit_percent or 0.0),
    ) / 100.0

    low = entry
    high = max(entry + TICK_SIZE, entry * 1.10)

    for _ in range(20):
        if _costs_and_net(entry, high)["net_pnl"] >= target_net:
            break
        high = high * 1.25 + TICK_SIZE

    for _ in range(60):
        middle = (low + high) / 2.0
        if _costs_and_net(entry, middle)["net_pnl"] >= target_net:
            high = middle
        else:
            low = middle

    solved = _round_up_tick(high)
    for _ in range(20):
        result = _costs_and_net(entry, solved)
        if result["net_pnl"] >= target_net:
            break
        solved = round(solved + TICK_SIZE, 2)

    final = _costs_and_net(entry, solved)
    return {
        "price": solved,
        "target_net_profit": round(target_net, 2),
        "net_pnl_at_price": final["net_pnl"],
        "total_charges_at_price": final["total_charges"],
        "net_profit_percent": round(float(net_profit_percent or 0.0), 2),
        "quantity_basis": _CONSERVATIVE_QTY,
        "basis": "CONSERVATIVE_SMALLEST_LOT_NSE_COSTS",
    }


def calculate_option_atr_levels(
    spot_price,
    option_entry_price,
    spot_atr,
    is_expiry_day=False,
    sl_floor_percent=0.0,
    reward_multiple=0.0,
):
    entry = max(TICK_SIZE, float(option_entry_price or TICK_SIZE))
    atr = max(0.0, float(spot_atr or 0))
    if is_expiry_day:
        response = 1.0
        multiplier = 1.5
        mode = "EXPIRY_PURE_ATR"
    else:
        response = 0.5
        multiplier = 1.2
        mode = "NORMAL_PURE_ATR"

    option_atr = atr * response
    raw_risk = option_atr * multiplier
    premium_risk_cap = entry * MAX_PREMIUM_RISK_PERCENT / 100.0
    risk = min(
        max(TICK_SIZE, raw_risk),
        max(TICK_SIZE, premium_risk_cap),
        max(TICK_SIZE, entry - TICK_SIZE),
    )
    return {
        "mode": mode,
        "spot_price": round(float(spot_price or 0), 2),
        "spot_atr": round(atr, 2),
        "atr_available": atr > 0,
        "response_factor": response,
        "estimated_option_atr": round(option_atr, 2),
        "atr_multiplier": multiplier,
        "atr_risk_points": round(raw_risk, 2),
        "percentage_risk_points": round(premium_risk_cap, 2),
        "sl_floor_percent": 0.0,
        "risk_points": round(risk, 2),
        "sl_price": round(max(TICK_SIZE, entry - risk), 2),
        "target_price": None,
        "reward_multiple": None,
        "is_expiry_day": bool(is_expiry_day),
        "fixed_target_enabled": False,
        "hard_premium_risk_cap_percent": MAX_PREMIUM_RISK_PERCENT,
        "hard_risk_cap_applied": bool(risk + 1e-9 < raw_risk),
        "quantity_preserved": True,
    }


def update_option_profit_lock(
    entry_price,
    initial_risk,
    current_sl,
    peak_price,
    current_price,
):
    entry = max(TICK_SIZE, float(entry_price or TICK_SIZE))
    risk = max(TICK_SIZE, float(initial_risk or TICK_SIZE))
    old_sl = max(TICK_SIZE, float(current_sl or entry - risk))
    current = max(TICK_SIZE, float(current_price or TICK_SIZE))
    peak = max(entry, float(peak_price or entry), current)
    peak_r = (peak - entry) / risk
    peak_r = round(float(peak_r), 10)

    true_be = calculate_cost_safe_breakeven_price(
        entry,
        TRUE_BE_NET_PROFIT_PERCENT,
    )
    be_price = float(true_be["price"])

    new_sl = old_sl
    stage = "INITIAL_ATR"
    locked_r = -1.0
    breakeven_triggered = peak + 1e-9 >= be_price + TICK_SIZE

    # First trail is not raw entry. It activates only after the premium has
    # enough room to lock entry + conservative all-in costs + 2% net profit.
    if breakeven_triggered:
        new_sl = max(new_sl, be_price)
        stage = "COST_PLUS_2PCT_BREAKEVEN"
        locked_r = (new_sl - entry) / risk

        if peak_r >= 1.2:
            new_sl = max(new_sl, entry + 0.5 * risk)
            stage = "LOCK_0_5R_AFTER_TRUE_BE"
            locked_r = (new_sl - entry) / risk

        if peak_r >= 1.8:
            new_sl = max(
                new_sl,
                entry + risk,
                peak - 0.8 * risk,
            )
            stage = "DYNAMIC_PROFIT_LOCK_AFTER_TRUE_BE"
            locked_r = (new_sl - entry) / risk

    peak_room = max(TICK_SIZE, peak - TICK_SIZE)
    candidate = min(new_sl, peak_room)
    if breakeven_triggered and candidate + 1e-9 < be_price:
        candidate = old_sl
        stage = "WAITING_TRUE_BE_PRICE_ROOM"
        locked_r = (candidate - entry) / risk
        breakeven_triggered = False
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
        "breakeven_triggered": bool(breakeven_triggered),
        "breakeven_rule": "ENTRY_PLUS_ALL_COSTS_PLUS_2PCT_NET",
        "breakeven_target_net_profit": true_be["target_net_profit"],
        "breakeven_net_pnl_at_stop": true_be["net_pnl_at_price"],
        "breakeven_total_charges": true_be["total_charges_at_price"],
    }


def detect_structural_reversal(
    position_side,
    price,
    vwap,
    ema9,
    ema21,
    supertrend_dir,
    opposite_signal=None,
    opposite_score=0,
    min_score=82,
):
    """
    Two-candle caller confirmation is still required.

    A reversal is valid only when price breaks VWAP and EMA9,
    plus either:
      1. Supertrend and EMA trend both flip opposite, or
      2. A fresh valid opposite-side signal scores at least 82.
    """
    side = str(position_side or "").upper()
    close = float(price or 0)
    vwap_value = float(vwap or close)
    ema9_value = float(ema9 or close)
    ema21_value = float(ema21 or ema9_value)
    st = str(supertrend_dir or "NEUTRAL").upper()
    signal = str(opposite_signal or "WAIT").upper()
    score = float(opposite_score or 0)
    score_gate = float(min_score or 82)

    if side == "CE":
        vwap_broken = close < vwap_value
        ema9_broken = close < ema9_value
        trend_flip_confirmed = (
            st == "DOWN"
            and ema9_value < ema21_value
        )
        valid_opposite_signal = (
            signal == "PE"
            and score >= score_gate
        )
    elif side == "PE":
        vwap_broken = close > vwap_value
        ema9_broken = close > ema9_value
        trend_flip_confirmed = (
            st == "UP"
            and ema9_value > ema21_value
        )
        valid_opposite_signal = (
            signal == "CE"
            and score >= score_gate
        )
    else:
        return {
            "detected": False,
            "side": side,
            "vwap_broken": False,
            "ema9_broken": False,
            "opposite_confirmed": False,
            "trend_flip_confirmed": False,
            "valid_opposite_signal": False,
            "opposite_signal": signal,
            "opposite_score": round(score, 2),
            "min_score": round(score_gate, 2),
        }

    opposite_confirmed = (
        trend_flip_confirmed
        or valid_opposite_signal
    )

    detected = (
        vwap_broken
        and ema9_broken
        and opposite_confirmed
    )

    return {
        "detected": bool(detected),
        "side": side,
        "vwap_broken": bool(vwap_broken),
        "ema9_broken": bool(ema9_broken),
        "opposite_confirmed": bool(opposite_confirmed),
        "trend_flip_confirmed": bool(
            trend_flip_confirmed
        ),
        "valid_opposite_signal": bool(
            valid_opposite_signal
        ),
        "opposite_signal": signal,
        "opposite_score": round(score, 2),
        "min_score": round(score_gate, 2),
        "price": round(close, 2),
        "vwap": round(vwap_value, 2),
        "ema9": round(ema9_value, 2),
        "ema21": round(ema21_value, 2),
        "supertrend_dir": st,
    }
