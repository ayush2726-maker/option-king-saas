def calculate_option_atr_levels(
    spot_price,
    option_entry_price,
    spot_atr,
    is_expiry_day=False,
    sl_floor_percent=0.0,
    reward_multiple=0.0,
):
    entry = max(0.05, float(option_entry_price or 0.05))
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
    risk = min(
        max(0.05, raw_risk),
        max(0.05, entry - 0.05),
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
        "percentage_risk_points": 0.0,
        "sl_floor_percent": 0.0,
        "risk_points": round(risk, 2),
        "sl_price": round(max(0.05, entry - risk), 2),
        "target_price": None,
        "reward_multiple": None,
        "is_expiry_day": bool(is_expiry_day),
        "fixed_target_enabled": False,
    }


def update_option_profit_lock(
    entry_price,
    initial_risk,
    current_sl,
    peak_price,
    current_price,
):
    entry = max(0.05, float(entry_price or 0.05))
    risk = max(0.05, float(initial_risk or 0.05))
    old_sl = max(0.05, float(current_sl or entry - risk))
    current = max(0.05, float(current_price or 0.05))
    peak = max(entry, float(peak_price or entry), current)
    peak_r = (peak - entry) / risk
    peak_r = round(float(peak_r), 10)

    new_sl = old_sl
    stage = "INITIAL_ATR"
    locked_r = -1.0

    if peak_r >= 0.8:
        new_sl = max(new_sl, entry)
        stage = "BREAKEVEN"
        locked_r = 0.0

    if peak_r >= 1.2:
        new_sl = max(new_sl, entry + 0.5 * risk)
        stage = "LOCK_0_5R"
        locked_r = 0.5

    if peak_r >= 1.8:
        new_sl = max(
            new_sl,
            entry + risk,
            peak - 0.8 * risk,
        )
        stage = "DYNAMIC_PROFIT_LOCK"
        locked_r = (new_sl - entry) / risk

    new_sl = min(new_sl, max(0.05, peak - 0.05))
    return {
        "sl_price": round(new_sl, 2),
        "old_sl_price": round(old_sl, 2),
        "updated": new_sl > old_sl + 1e-9,
        "peak_price": round(peak, 2),
        "peak_r": round(peak_r, 2),
        "locked_r": round(locked_r, 2),
        "stage": stage,
        "initial_risk": round(risk, 2),
    }
