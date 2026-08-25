"""Adaptive ORB neutral scoring for OKAI Default 82.

ORB is a quality confirmation, not a mandatory entry gate. When ORB data is
missing (high/low are zero) or when the active price is materially far away from
the ORB zone, the ORB leg is treated as not-applicable instead of a failed 0/11
confirmation. This is especially important on gap-up/gap-down and strong opening
move days where a later valid reversal/continuation setup can be far from the
opening range. The protected threshold remains 82; only the base-score
denominator changes from 5 to 4 while ORB is not applicable.
"""
from __future__ import annotations

from typing import Any

ORB_BUFFER_POINTS = 5.0
ORB_FAR_ATR_MULTIPLIER = 1.5
ORB_FAR_MIN_POINTS = 40.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _orb_breakout_direction(price: float, orb_high: float, orb_low: float) -> str:
    if orb_high > 0 and price > orb_high + ORB_BUFFER_POINTS:
        return "CE"
    if orb_low > 0 and price < orb_low - ORB_BUFFER_POINTS:
        return "PE"
    return "WAIT"


def _orb_available(orb_high: float, orb_low: float) -> bool:
    return bool(orb_high > 0 and orb_low > 0 and orb_high > orb_low)


def _orb_far_from_active_price(price: float, orb_high: float, orb_low: float, spot_atr: float) -> bool:
    """Return True when ORB is too far away to be a useful current confirmation.

    Do not require an explicit ``gap_day`` flag here. The replay-first live path
    historically hard-coded that flag to False, which made the old gap-specific
    protection ineffective even when the opening range was obviously far away.
    Distance itself is the reliable runtime fact we need for scoring.
    """
    if not _orb_available(orb_high, orb_low):
        return False

    nearest_distance = min(abs(price - orb_high), abs(price - orb_low))
    far_threshold = max(
        ORB_FAR_MIN_POINTS,
        max(0.0, spot_atr) * ORB_FAR_ATR_MULTIPLIER,
    )
    return nearest_distance > far_threshold


def _dynamic_base_score(base_points: int, denominator: int) -> int:
    denominator = max(1, int(denominator))
    return int((float(base_points) / float(denominator)) * 55.0)


def calculate_base_score_orb_neutral(
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
    gap_day: bool = False,
    spot_atr: float = 0.0,
) -> dict:
    ce_score = 0
    pe_score = 0

    if price > vwap:
        ce_score += 1
    else:
        pe_score += 1

    supertrend = str(supertrend_dir or "NEUTRAL").upper()
    trend_text = str(trend or "SIDEWAYS").upper()
    if supertrend == "UP":
        ce_score += 1
    elif supertrend == "DOWN":
        pe_score += 1

    if ema9 > ema21 and trend_text == "UPTREND":
        ce_score += 1
    if ema9 < ema21 and trend_text == "DOWNTREND":
        pe_score += 1

    price = _f(price)
    orb_high = _f(orb_high)
    orb_low = _f(orb_low)
    spot_atr = _f(spot_atr)
    orb_available = _orb_available(orb_high, orb_low)
    orb_direction = _orb_breakout_direction(price, orb_high, orb_low) if orb_available else "NA"
    orb_far = _orb_far_from_active_price(price, orb_high, orb_low, spot_atr)

    # First determine the active side from the four non-ORB confirmations.
    # A distant ORB is observation-only only when it is neutral or agrees with
    # that side.  If price is far beyond the opposite ORB boundary, dropping the
    # ORB denominator would incorrectly promote a counter-trend 4/5 setup to a
    # perfect 4/4 setup (44 -> 55 base points).  Preserve the conflicting ORB
    # vote so a bullish session cannot inflate a PE score, and vice versa.
    if ce_score > pe_score:
        non_orb_side = "CE"
    elif pe_score > ce_score:
        non_orb_side = "PE"
    else:
        non_orb_side = "WAIT"
    orb_conflicts_with_active_side = bool(
        orb_available
        and orb_direction in {"CE", "PE"}
        and non_orb_side in {"CE", "PE"}
        and orb_direction != non_orb_side
    )
    orb_applicable = bool(
        orb_available
        and (not orb_far or orb_conflicts_with_active_side)
    )

    if orb_applicable:
        if orb_direction == "CE":
            ce_score += 1
        elif orb_direction == "PE":
            pe_score += 1

    if c1_bullish and c2_bullish:
        ce_score += 1
    if not c1_bullish and not c2_bullish:
        pe_score += 1

    if ce_score > pe_score:
        signal = "CE"
        base = ce_score
    elif pe_score > ce_score:
        signal = "PE"
        base = pe_score
    else:
        signal = "WAIT"
        base = 0

    denominator = 5 if orb_applicable else 4
    normalized = _dynamic_base_score(base, denominator) if signal in ("CE", "PE") else 0
    reasons = []
    if not orb_available:
        reasons.append("ORB_NOT_APPLICABLE_UNAVAILABLE")
    elif orb_far and not orb_conflicts_with_active_side:
        reasons.append("ORB_NOT_APPLICABLE_FAR")
    elif orb_far and orb_conflicts_with_active_side:
        reasons.append("ORB_FAR_OPPOSITE_DIRECTION_RETAINED")
    elif orb_direction == "WAIT":
        reasons.append("ORB_NEUTRAL_NO_BREAKOUT")

    return {
        "signal": signal,
        "ce_score": ce_score,
        "pe_score": pe_score,
        "base_score": normalized,
        "orb_applicable": orb_applicable,
        "orb_available": orb_available,
        "orb_far_due_to_gap": orb_far,
        "orb_conflicts_with_active_side": orb_conflicts_with_active_side,
        "orb_direction": orb_direction,
        "orb_score_denominator": denominator,
        "orb_neutral_reasons": reasons,
        "gap_day": bool(gap_day),
    }


def apply_orb_gap_neutral_scoring_patch() -> None:
    try:
        from bot import strategy

        if getattr(strategy, "_okai_orb_gap_neutral_scoring_v1", False):
            return

        original_get_full_signal = strategy.get_full_signal

        def get_full_signal_with_orb_neutral(market_data: dict, consecutive_losses: int = 0, profile: dict = None) -> dict:
            original_calculate = strategy.calculate_base_score
            try:
                def patched_calculate_base_score(
                    price, vwap, ema9, ema21, supertrend_dir, trend,
                    orb_high, orb_low, c1_bullish, c2_bullish,
                ):
                    market = market_data or {}
                    return calculate_base_score_orb_neutral(
                        price, vwap, ema9, ema21, supertrend_dir, trend,
                        orb_high, orb_low, c1_bullish, c2_bullish,
                        gap_day=bool(market.get("gap_day", False)),
                        spot_atr=_f(market.get("atr", 0.0)),
                    )

                strategy.calculate_base_score = patched_calculate_base_score
                result = dict(original_get_full_signal(market_data, consecutive_losses=consecutive_losses, profile=profile) or {})
            finally:
                strategy.calculate_base_score = original_calculate

            base = calculate_base_score_orb_neutral(
                _f((market_data or {}).get("price")),
                _f((market_data or {}).get("vwap"), _f((market_data or {}).get("price"))),
                _f((market_data or {}).get("ema9"), _f((market_data or {}).get("price"))),
                _f((market_data or {}).get("ema21"), _f((market_data or {}).get("price"))),
                str((market_data or {}).get("supertrend_dir", "NEUTRAL")),
                str((market_data or {}).get("trend", "SIDEWAYS")),
                _f((market_data or {}).get("orb_high")),
                _f((market_data or {}).get("orb_low")),
                bool((market_data or {}).get("c1_bullish", False)),
                bool((market_data or {}).get("c2_bullish", False)),
                gap_day=bool((market_data or {}).get("gap_day", False)),
                spot_atr=_f((market_data or {}).get("atr", 0.0)),
            )
            warnings = list(result.get("warnings") or [])
            for reason in base.get("orb_neutral_reasons") or []:
                if reason not in warnings:
                    warnings.append(reason)
            result["warnings"] = warnings
            result["orb_applicable"] = base.get("orb_applicable")
            result["orb_available"] = base.get("orb_available")
            result["orb_far_due_to_gap"] = base.get("orb_far_due_to_gap")
            result["orb_score_denominator"] = base.get("orb_score_denominator")
            result["orb_neutral_reasons"] = base.get("orb_neutral_reasons")
            return result

        strategy.get_full_signal = get_full_signal_with_orb_neutral
        try:
            from bot import routes
            routes.get_full_signal = get_full_signal_with_orb_neutral
        except Exception:
            pass
        try:
            from bot import angel_fetcher
            angel_fetcher.get_full_signal = get_full_signal_with_orb_neutral
        except Exception:
            pass
        strategy._okai_orb_gap_neutral_scoring_v1 = True
    except Exception:
        pass
