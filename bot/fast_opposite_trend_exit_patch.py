"""Fast opposite-trend exit for AUTO Portfolio positions.

If an open CE/PE is invalidated by a confirmed opposite market move, exit it
without waiting for the option-premium stop-loss. Uses completed-candle market
state already produced by the scan. This does not auto-open the opposite side;
normal entry gates remain responsible for any new trade.
"""
from __future__ import annotations

from bot import auto_portfolio_runtime as runtime

VERSION = "OKAI-FAST-OPPOSITE-TREND-EXIT-V1"


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _opposite_exit_state(trade, market_data):
    market = dict(market_data or {})
    side = str(runtime._v(trade, "side", "") or "").upper()
    if side not in {"CE", "PE"} or not market:
        return {"exit": False, "version": VERSION, "reason": "NO_MARKET_STATE"}

    price = _f(market.get("price"), 0.0)
    vwap = _f(market.get("vwap"), price)
    ema9 = _f(market.get("ema9"), price)
    ema21 = _f(market.get("ema21"), ema9)
    adx = _f(market.get("adx"), 0.0)
    st = str(market.get("supertrend_dir") or "NEUTRAL").upper()
    signal = str(market.get("signal") or "WAIT").upper()
    score = _f(market.get("signal_score"), 0.0)
    min_score = _f(market.get("signal_min_score"), 82.0)

    opposite = "PE" if side == "CE" else "CE"

    if side == "PE":
        price_vwap_flip = price > vwap
        ema_flip = ema9 > ema21
        st_flip = st == "UP"
    else:
        price_vwap_flip = price < vwap
        ema_flip = ema9 < ema21
        st_flip = st == "DOWN"

    # Strongest proof: the normal strategy itself has produced a qualified
    # opposite-side signal. This is enough to invalidate the held direction.
    qualified_opposite_signal = signal == opposite and score >= min_score

    # Backup proof: even before a full opposite score is available, a completed
    # candle showing VWAP + EMA + Supertrend all flipped with meaningful trend
    # strength is treated as a genuine direction change rather than noise.
    structural_flip = (
        price_vwap_flip
        and ema_flip
        and st_flip
        and adx >= 22.0
    )

    should_exit = bool(qualified_opposite_signal or structural_flip)
    return {
        "exit": should_exit,
        "version": VERSION,
        "held_side": side,
        "opposite_side": opposite,
        "qualified_opposite_signal": bool(qualified_opposite_signal),
        "structural_flip": bool(structural_flip),
        "price_vwap_flip": bool(price_vwap_flip),
        "ema_flip": bool(ema_flip),
        "supertrend_flip": bool(st_flip),
        "adx": round(adx, 2),
        "signal": signal,
        "score": round(score, 2),
        "min_score": round(min_score, 2),
        "reason": (
            "QUALIFIED_OPPOSITE_SIGNAL"
            if qualified_opposite_signal
            else "STRONG_STRUCTURAL_TREND_FLIP"
            if structural_flip
            else "NO_CONFIRMED_OPPOSITE_TREND"
        ),
    }


def apply_fast_opposite_trend_exit_patch() -> None:
    if getattr(runtime, "_okai_fast_opposite_trend_exit_v1", False):
        return

    original_evaluate_exit = runtime._evaluate_exit

    def evaluate_exit_with_fast_opposite(trade, ltp, market_data, candle_id):
        result = dict(original_evaluate_exit(trade, ltp, market_data, candle_id) or {})

        # Existing SL/profit-lock/EOD reasons keep priority. Only add the fast
        # reversal exit when the trade would otherwise stay open.
        if result.get("reason"):
            return result

        fast = _opposite_exit_state(trade, market_data)
        result["fast_opposite_exit"] = fast
        if fast.get("exit"):
            result["reason"] = (
                "FAST OPPOSITE TREND EXIT"
                f" | {fast.get('reason')}"
                f" | {fast.get('held_side')}->{fast.get('opposite_side')}"
                f" | ADX={fast.get('adx')}"
            )
        return result

    runtime._evaluate_exit = evaluate_exit_with_fast_opposite
    runtime._okai_fast_opposite_trend_exit_v1 = True
