"""
Option King AI - SaaS Strategy Core
Same logic as personal bot app.py
weighted_min_entry_score = 82 (PROTECTED - never change)
"""

from datetime import datetime, timezone, timedelta

# ── PROTECTED CONSTANTS ───────────────────────────────────
WEIGHTED_MIN_ENTRY_SCORE = 82      # NEVER CHANGE
ADX_THRESHOLD = 25.0
VOLUME_RATIO_THRESHOLD = 1.2
VOLUME_NEUTRAL_BONUS = 7  # half-weight when index volume is unavailable
SIDEWAYS_THRESHOLD = 70
LOSS_COOLDOWN_SECONDS = 15 * 60   # 15 min
HERO_CAPITAL_CAP = 2000
HERO_WINDOW_START = (14, 30)       # 14:30 IST
HERO_WINDOW_END   = (15, 0)        # 15:00 IST
HERO_FORCE_EXIT   = (15, 25)       # 15:25 IST

# ── TQU Score Calculation ────────────────────────────────
def calculate_tqu_score(
    base_score: float,
    adx: float,
    volume_ratio: float,
    mtf_confirmed: bool,
    is_sideways: bool,
    gap_day: bool = False,
    volume_available: bool = True,
) -> dict:
    """
    Trade Quality Upgrade (TQU) system
    Same as personal bot - ADX + Volume + MTF filters
    """
    score = float(base_score)
    adx_bonus = 0
    volume_bonus = 0
    mtf_bonus = 0
    regime_score = 0
    warnings = []

    # Sideways market guard
    if is_sideways:
        score = min(score, SIDEWAYS_THRESHOLD)
        warnings.append("SIDEWAYS_MARKET_GUARD_ACTIVE")

    # ADX filter (28+ candles required)
    if adx > 0:
        if adx >= ADX_THRESHOLD:
            adx_bonus = min(20, int((adx - ADX_THRESHOLD) * 0.8 + 10))
            score += adx_bonus
            regime_score += 5
        else:
            warnings.append(f"ADX_WEAK:{adx:.1f}<{ADX_THRESHOLD}")

    # Volume confirmation. NSE index candles may legitimately have no
    # volume. Treat missing volume as neutral half-weight rather than a hard
    # scoring disadvantage; never invent a high volume ratio.
    if not volume_available:
        volume_bonus = VOLUME_NEUTRAL_BONUS
        score += volume_bonus
        warnings.append("VOLUME_UNAVAILABLE_NEUTRAL")
    elif volume_ratio > 0:
        if volume_ratio >= VOLUME_RATIO_THRESHOLD:
            volume_bonus = min(15, int((volume_ratio - 1.0) * 10))
            score += volume_bonus
        else:
            warnings.append(f"VOLUME_LOW:{volume_ratio:.2f}x")

    # MTF confirmation (5m - warning only for strong setups)
    if mtf_confirmed:
        mtf_bonus = 10
        score += mtf_bonus
    else:
        warnings.append("MTF_5M_NOT_CONFIRMED")

    # Gap day adjustment
    if gap_day:
        regime_score -= 5
        warnings.append("GAP_DAY_CAUTION")

    final_score = min(100, max(0, int(score)))

    return {
        "score": final_score,
        "base_score": int(base_score),
        "adx_bonus": adx_bonus,
        "volume_bonus": volume_bonus,
        "volume_available": bool(volume_available),
        "mtf_bonus": mtf_bonus,
        "regime_score": max(0, regime_score),
        "warnings": warnings,
        "trade_allowed": final_score >= WEIGHTED_MIN_ENTRY_SCORE,
        "min_score_required": WEIGHTED_MIN_ENTRY_SCORE,
    }

# ── Signal Logic (same as app.py choose_rule_signal) ────
def calculate_base_score(
    price: float,
    vwap: float,
    ema9: float,
    ema21: float,
    supertrend_dir: str,
    trend: str,
    orb_high: float,
    orb_low: float,
    c1_bullish: bool,
    c2_bullish: bool,
) -> dict:
    """
    Base CE/PE score — same as personal bot logic
    """
    ce_score = 0
    pe_score = 0

    # VWAP filter
    if price > vwap:
        ce_score += 1
    else:
        pe_score += 1

    # Supertrend
    if supertrend_dir == "UP":
        ce_score += 1
    elif supertrend_dir == "DOWN":
        pe_score += 1

    # EMA + Trend
    if ema9 > ema21 and trend == "UPTREND":
        ce_score += 1
    if ema9 < ema21 and trend == "DOWNTREND":
        pe_score += 1

    # ORB rules
    orb_buffer = 5  # points
    if orb_high > 0 and price > (orb_high + orb_buffer):
        ce_score += 1
    if orb_low > 0 and price < (orb_low - orb_buffer):
        pe_score += 1

    # Two candle momentum
    if c1_bullish and c2_bullish:
        ce_score += 1
    if not c1_bullish and not c2_bullish:
        pe_score += 1

    # Signal decision
    if ce_score > pe_score:
        signal = "CE"
        base = ce_score
    elif pe_score > ce_score:
        signal = "PE"
        base = pe_score
    else:
        signal = "WAIT"
        base = 0

    # Base contributes maximum 55 points.
    # TQU bonuses contribute maximum 45 points:
    # ADX 20 + Volume 15 + MTF 10 = total score maximum 100.
    normalized = int((base / 5) * 55)

    return {
        "signal": signal,
        "ce_score": ce_score,
        "pe_score": pe_score,
        "base_score": normalized,
    }

# ── HERO ZERO EXPIRY Strategy ────────────────────────────
def is_hero_window_active() -> dict:
    """
    HERO_ZERO_EXPIRY: 14:30-15:00 IST on expiry days
    Force exit at 15:25 IST
    Capital cap: Rs 2000
    """
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)

    h, m = ist.hour, ist.minute
    total_min = h * 60 + m

    window_start = HERO_WINDOW_START[0] * 60 + HERO_WINDOW_START[1]
    window_end   = HERO_WINDOW_END[0]   * 60 + HERO_WINDOW_END[1]
    force_exit   = HERO_FORCE_EXIT[0]   * 60 + HERO_FORCE_EXIT[1]

    active = window_start <= total_min < window_end
    in_force_exit = window_end <= total_min < force_exit

    if total_min < window_start:
        diff = window_start - total_min
        status = f"Opens in {diff//60}h {diff%60}m"
    elif active:
        diff = window_end - total_min
        status = f"ACTIVE — {diff}m remaining"
    elif in_force_exit:
        diff = force_exit - total_min
        status = f"Force exit in {diff}m"
    else:
        status = "Window closed for today"

    return {
        "active": active,
        "in_force_exit": in_force_exit,
        "status": status,
        "capital_cap": HERO_CAPITAL_CAP,
        "ist_time": ist.strftime("%H:%M:%S"),
    }

# ── ATR Dynamic SL ───────────────────────────────────────
def calculate_atr_sl(
    price: float,
    atr: float,
    signal: str,
    multiplier: float = 1.5,
) -> dict:
    """ATR-based dynamic stop loss — same as personal bot"""
    sl_points = atr * multiplier
    if signal == "CE":
        sl = price - sl_points
        target = price + (sl_points * 2)
    else:
        sl = price + sl_points
        target = price - (sl_points * 2)

    return {
        "sl": round(sl, 2),
        "target": round(target, 2),
        "sl_points": round(sl_points, 2),
        "risk_reward": 2.0,
    }

from bot.dynamic_exit import (
    calculate_option_atr_levels,
    update_option_profit_lock,
)


# ── Loss Cooldown Check ──────────────────────────────────
def is_in_loss_cooldown(last_loss_ts: float) -> bool:
    """15-minute cooldown after loss — same as personal bot"""
    if not last_loss_ts:
        return False
    now = datetime.now(timezone.utc).timestamp()
    return (now - last_loss_ts) < LOSS_COOLDOWN_SECONDS

# ── Trailing SL Update ───────────────────────────────────
def update_trailing_sl(
    current_price: float,
    entry_price: float,
    current_sl: float,
    signal: str,
    trail_points: float,
) -> dict:
    """Trailing SL logic — same as personal bot"""
    new_sl = current_sl
    updated = False

    if signal == "CE":
        potential_sl = current_price - trail_points
        if potential_sl > current_sl:
            new_sl = potential_sl
            updated = True
    else:
        potential_sl = current_price + trail_points
        if potential_sl < current_sl:
            new_sl = potential_sl
            updated = True

    return {
        "sl": round(new_sl, 2),
        "updated": updated,
    }

# ── Full Signal Pipeline ─────────────────────────────────
# ── ATR-adaptive anti-chase thresholds ───────────────────
# NIFTY keeps the existing minimum protection.
# Higher-volatility indices receive wider limits automatically.
EMA_STRETCH_BLOCK_POINTS = 22.0
VWAP_STRETCH_BLOCK_POINTS = 35.0
EMA_STRETCH_ATR_MULTIPLIER = 1.2
VWAP_STRETCH_ATR_MULTIPLIER = 2.0


def calculate_anti_chase_limits(spot_atr: float) -> dict:
    # Return volatility-adaptive EMA/VWAP chase limits.
    try:
        atr = max(0.0, float(spot_atr or 0.0))
    except (TypeError, ValueError):
        atr = 0.0

    ema_limit = max(
        EMA_STRETCH_BLOCK_POINTS,
        atr * EMA_STRETCH_ATR_MULTIPLIER,
    )
    vwap_limit = max(
        VWAP_STRETCH_BLOCK_POINTS,
        atr * VWAP_STRETCH_ATR_MULTIPLIER,
    )

    return {
        "spot_atr": round(atr, 2),
        "ema_limit": round(ema_limit, 2),
        "vwap_limit": round(vwap_limit, 2),
        "mode": (
            "ATR_ADAPTIVE"
            if atr > 0
            else "FIXED_FALLBACK"
        ),
    }


def required_score_for_losses(consecutive_losses: int) -> int:
    """Entry score remains protected at 82 after any number of losses."""
    return WEIGHTED_MIN_ENTRY_SCORE


def _custom_profile_signal(
    market_data,
    profile,
    consecutive_losses=0,
):
    price = float(market_data.get("price", 0))
    vwap = float(market_data.get("vwap", price))
    ema9 = float(market_data.get("ema9", price))
    ema21 = float(market_data.get("ema21", price))
    adx = float(market_data.get("adx", 0))
    volume_ratio = float(
        market_data.get("volume_ratio", 0)
    )
    volume_available = bool(
        market_data.get(
            "volume_available",
            volume_ratio > 0,
        )
    )
    supertrend = str(
        market_data.get(
            "supertrend_dir",
            "NEUTRAL",
        )
    ).upper()
    trend = str(
        market_data.get(
            "trend",
            "SIDEWAYS",
        )
    ).upper()
    mtf_ok = bool(
        market_data.get("mtf_confirmed", False)
    )
    orb_high = float(
        market_data.get("orb_high", 0)
    )
    orb_low = float(
        market_data.get("orb_low", 0)
    )
    c1_bull = bool(
        market_data.get("c1_bullish", False)
    )
    c2_bull = bool(
        market_data.get("c2_bullish", False)
    )
    spot_atr = max(
        0.0,
        float(market_data.get("atr", 0) or 0),
    )
    vwap_fallback_used = bool(
        market_data.get(
            "vwap_fallback_used",
            False,
        )
    )

    weights = dict(
        profile.get("weights", {})
    )
    enabled = dict(
        profile.get("enabled", {})
    )

    ce_checks = {
        "vwap": price > vwap,
        "supertrend": supertrend == "UP",
        "ema_trend": (
            ema9 > ema21
            and trend == "UPTREND"
        ),
        "orb": (
            orb_high > 0
            and price > orb_high + 5
        ),
        "momentum": c1_bull and c2_bull,
    }
    pe_checks = {
        "vwap": price < vwap,
        "supertrend": supertrend == "DOWN",
        "ema_trend": (
            ema9 < ema21
            and trend == "DOWNTREND"
        ),
        "orb": (
            orb_low > 0
            and price < orb_low - 5
        ),
        "momentum": (
            not c1_bull
            and not c2_bull
        ),
    }

    directional_keys = (
        "vwap",
        "supertrend",
        "ema_trend",
        "orb",
        "momentum",
    )

    ce_score = sum(
        int(weights.get(key, 0))
        for key in directional_keys
        if enabled.get(key, True)
        and ce_checks[key]
    )
    pe_score = sum(
        int(weights.get(key, 0))
        for key in directional_keys
        if enabled.get(key, True)
        and pe_checks[key]
    )

    ce_confirmations = sum(
        1
        for key in directional_keys
        if enabled.get(key, True)
        and ce_checks[key]
    )
    pe_confirmations = sum(
        1
        for key in directional_keys
        if enabled.get(key, True)
        and pe_checks[key]
    )

    if ce_score > pe_score:
        candidate = "CE"
        directional_score = ce_score
    elif pe_score > ce_score:
        candidate = "PE"
        directional_score = pe_score
    else:
        candidate = "WAIT"
        directional_score = 0

    warnings = []
    score = float(directional_score)

    adx_bonus = 0
    if enabled.get("adx", True):
        adx_threshold = float(
            profile.get("adx_threshold", 25)
        )
        if adx > 0 and adx >= adx_threshold:
            adx_bonus = int(
                weights.get("adx", 0)
            )
            score += adx_bonus
        else:
            warnings.append(
                f"ADX_WEAK:{adx:.1f}<"
                f"{adx_threshold:.1f}"
            )

    volume_bonus = 0
    if enabled.get("volume", True):
        volume_threshold = float(
            profile.get(
                "volume_threshold",
                1.2,
            )
        )
        if not volume_available:
            volume_bonus = max(
                0,
                int(round(float(weights.get("volume", 0)) * 0.5)),
            )
            score += volume_bonus
            warnings.append("VOLUME_UNAVAILABLE_NEUTRAL")
        elif (
            volume_ratio > 0
            and volume_ratio >= volume_threshold
        ):
            volume_bonus = int(
                weights.get("volume", 0)
            )
            score += volume_bonus
        else:
            warnings.append(
                f"VOLUME_LOW:{volume_ratio:.2f}x"
            )

    mtf_bonus = 0
    if enabled.get("mtf", True):
        if mtf_ok:
            mtf_bonus = int(
                weights.get("mtf", 0)
            )
            score += mtf_bonus
        else:
            warnings.append(
                "MTF_NOT_CONFIRMED"
            )

    sideways_mode = str(
        profile.get("sideways_mode", "cap")
    ).lower()
    sideways_blocked = False

    if trend == "SIDEWAYS":
        if sideways_mode == "block":
            sideways_blocked = True
            warnings.append(
                "SIDEWAYS_MARKET_BLOCKED"
            )
        elif sideways_mode == "cap":
            score = min(score, 70)
            warnings.append(
                "SIDEWAYS_SCORE_CAP_70"
            )
        else:
            warnings.append(
                "SIDEWAYS_ALLOWED"
            )

    anti = dict(
        profile.get("anti_chase", {})
    )

    ema_limit = max(
        float(
            anti.get("ema_min_points", 22)
        ),
        spot_atr
        * float(
            anti.get(
                "ema_atr_multiplier",
                1.2,
            )
        ),
    )
    vwap_limit = max(
        float(
            anti.get("vwap_min_points", 35)
        ),
        spot_atr
        * float(
            anti.get(
                "vwap_atr_multiplier",
                2.0,
            )
        ),
    )

    ema_stretch = (
        abs(price - ema9) if ema9 else 0.0
    )
    vwap_stretch = (
        abs(price - vwap) if vwap else 0.0
    )

    ema_chase_blocked = (
        bool(anti.get("ema_enabled", True))
        and ema_stretch > ema_limit
    )
    vwap_chase_enabled = (
        bool(
            anti.get("vwap_enabled", True)
        )
        and not vwap_fallback_used
    )
    vwap_chase_blocked = (
        vwap_chase_enabled
        and vwap_stretch > vwap_limit
    )
    chase_blocked = (
        ema_chase_blocked
        or vwap_chase_blocked
    )

    if vwap_fallback_used:
        warnings.append(
            "VWAP_FALLBACK_ACTIVE:"
            "VWAP_CHASE_DISABLED"
        )
    if ema_chase_blocked:
        warnings.append(
            "CUSTOM_ANTI_CHASE_EMA"
        )
    if vwap_chase_blocked:
        warnings.append(
            "CUSTOM_ANTI_CHASE_VWAP"
        )

    final_score = min(
        100,
        max(0, int(round(score))),
    )
    required_score = int(
        profile.get("entry_threshold", 82)
    )
    trade_allowed = (
        candidate in ("CE", "PE")
        and final_score >= required_score
        and not chase_blocked
        and not sideways_blocked
    )

    return {
        "signal": (
            candidate if trade_allowed else "WAIT"
        ),
        "candidate_signal": candidate,
        "ce_raw_score": ce_confirmations,
        "pe_raw_score": pe_confirmations,
        "score": final_score,
        "base_score": int(directional_score),
        "adx": adx,
        "adx_bonus": adx_bonus,
        "volume_ratio": volume_ratio,
        "volume_available": bool(volume_available),
        "volume_bonus": volume_bonus,
        "mtf_confirmed": mtf_ok,
        "mtf_bonus": mtf_bonus,
        "regime_score": 0,
        "trade_allowed": trade_allowed,
        "min_score": required_score,
        "ema_stretch_points": round(
            ema_stretch,
            1,
        ),
        "vwap_stretch_points": round(
            vwap_stretch,
            1,
        ),
        "spot_atr": round(spot_atr, 2),
        "ema_stretch_limit": round(
            ema_limit,
            2,
        ),
        "vwap_stretch_limit": round(
            vwap_limit,
            2,
        ),
        "anti_chase_mode": (
            "CUSTOM_ATR_ADAPTIVE"
        ),
        "vwap_fallback_used": (
            vwap_fallback_used
        ),
        "vwap_chase_enabled": (
            vwap_chase_enabled
        ),
        "ema_chase_blocked": (
            ema_chase_blocked
        ),
        "vwap_chase_blocked": (
            vwap_chase_blocked
        ),
        "chase_blocked": chase_blocked,
        "sideways_blocked": (
            sideways_blocked
        ),
        "consecutive_losses": (
            consecutive_losses
        ),
        "warnings": warnings,
        "strategy": "CUSTOM_PROFILE_V1",
        "strategy_profile_key": (
            profile.get(
                "profile_key",
                "custom",
            )
        ),
        "strategy_profile_name": (
            profile.get(
                "profile_name",
                "Custom Strategy",
            )
        ),
        "profile_weights": weights,
        "profile_enabled": enabled,
        "score_breakdown": {
            "directional": int(
                directional_score
            ),
            "adx": adx_bonus,
            "volume": volume_bonus,
            "mtf": mtf_bonus,
        },
    }


def get_full_signal(
    market_data: dict,
    consecutive_losses: int = 0,
    profile: dict = None,
) -> dict:
    """
    Complete signal pipeline combining all strategies.

    OKAI Default 82 uses the protected original logic.
    Custom profiles use Strategy Builder V1 scoring.
    """
    profile_key = str(
        (profile or {}).get(
            "profile_key",
            "okai_default_82",
        )
    )

    if (
        profile
        and profile_key != "okai_default_82"
    ):
        return _custom_profile_signal(
            market_data,
            profile,
            consecutive_losses,
        )
    price        = float(market_data.get("price", 0))
    vwap         = float(market_data.get("vwap", price))
    ema9         = float(market_data.get("ema9", price))
    ema21        = float(market_data.get("ema21", price))
    adx          = float(market_data.get("adx", 0))
    volume_ratio = float(market_data.get("volume_ratio", 1.0))
    volume_available = bool(
        market_data.get(
            "volume_available",
            volume_ratio > 0,
        )
    )
    supertrend   = str(market_data.get("supertrend_dir", "NEUTRAL"))
    trend        = str(market_data.get("trend", "SIDEWAYS"))
    mtf_ok       = bool(market_data.get("mtf_confirmed", False))
    orb_high     = float(market_data.get("orb_high", 0))
    orb_low      = float(market_data.get("orb_low", 0))
    c1_bull      = bool(market_data.get("c1_bullish", False))
    c2_bull      = bool(market_data.get("c2_bullish", False))
    gap_day      = bool(market_data.get("gap_day", False))
    try:
        spot_atr = max(
            0.0,
            float(market_data.get("atr", 0) or 0),
        )
    except (TypeError, ValueError):
        spot_atr = 0.0
    vwap_fallback_used = bool(
        market_data.get("vwap_fallback_used", False)
    )
    is_sideways  = trend == "SIDEWAYS"

    # Step 1: Base score
    base = calculate_base_score(
        price, vwap, ema9, ema21,
        supertrend, trend, orb_high, orb_low,
        c1_bull, c2_bull,
    )

    # Step 2: TQU score
    tqu = calculate_tqu_score(
        base["base_score"], adx, volume_ratio,
        mtf_ok, is_sideways, gap_day,
        volume_available=volume_available,
    )

    # Step 3: Anti-chase gate
    #
    # EMA chase protection is always active.
    # VWAP chase protection is active only when true volume-weighted
    # VWAP is available. Session-average fallback is useful for
    # direction scoring, but should not act as a strict stretch guard.
    ema_stretch_points = abs(price - ema9) if ema9 else 0.0
    vwap_stretch_points = abs(price - vwap) if vwap else 0.0

    anti_chase_limits = calculate_anti_chase_limits(
        spot_atr
    )
    ema_stretch_limit = anti_chase_limits[
        "ema_limit"
    ]
    vwap_stretch_limit = anti_chase_limits[
        "vwap_limit"
    ]

    ema_chase_blocked = (
        ema_stretch_points > ema_stretch_limit
    )

    vwap_chase_enabled = not vwap_fallback_used
    vwap_chase_blocked = (
        vwap_chase_enabled
        and vwap_stretch_points > vwap_stretch_limit
    )

    chase_blocked = (
        ema_chase_blocked
        or vwap_chase_blocked
    )

    warnings = list(tqu["warnings"])

    if vwap_fallback_used:
        warnings.append(
            "VWAP_FALLBACK_ACTIVE:VWAP_CHASE_DISABLED"
        )

    if ema_chase_blocked:
        warnings.append(
            f"ANTI_CHASE_EMA_STRETCH:"
            f"{ema_stretch_points:.1f}pt>"
            f"{ema_stretch_limit:.1f}pt"
        )

    if vwap_chase_blocked:
        warnings.append(
            f"ANTI_CHASE_VWAP_STRETCH:"
            f"{vwap_stretch_points:.1f}pt>"
            f"{vwap_stretch_limit:.1f}pt"
        )

    # Step 4: Protected fixed score gate.
    # Consecutive losses never raise this to 85 or 87.
    required_score = WEIGHTED_MIN_ENTRY_SCORE
    score_ok = tqu["score"] >= required_score
    trade_allowed = score_ok and not chase_blocked

    # Step 5: Final decision
    signal = base["signal"] if trade_allowed else "WAIT"

    return {
        "signal": signal,
        "candidate_signal": base["signal"],
        "ce_raw_score": base["ce_score"],
        "pe_raw_score": base["pe_score"],
        "score": tqu["score"],
        "base_score": tqu["base_score"],
        "adx": adx,
        "adx_bonus": tqu["adx_bonus"],
        "volume_ratio": volume_ratio,
        "volume_available": bool(volume_available),
        "volume_bonus": tqu["volume_bonus"],
        "mtf_confirmed": mtf_ok,
        "mtf_bonus": tqu["mtf_bonus"],
        "regime_score": tqu["regime_score"],
        "trade_allowed": trade_allowed,
        "min_score": required_score,
        "ema_stretch_points": round(ema_stretch_points, 1),
        "vwap_stretch_points": round(vwap_stretch_points, 1),
        "spot_atr": round(spot_atr, 2),
        "ema_stretch_limit": round(
            ema_stretch_limit,
            2,
        ),
        "vwap_stretch_limit": round(
            vwap_stretch_limit,
            2,
        ),
        "anti_chase_mode": anti_chase_limits["mode"],
        "vwap_fallback_used": vwap_fallback_used,
        "vwap_chase_enabled": vwap_chase_enabled,
        "ema_chase_blocked": ema_chase_blocked,
        "vwap_chase_blocked": vwap_chase_blocked,
        "chase_blocked": chase_blocked,
        "consecutive_losses": consecutive_losses,
        "warnings": warnings,
        "strategy": "TQU_ENHANCED",
    }
