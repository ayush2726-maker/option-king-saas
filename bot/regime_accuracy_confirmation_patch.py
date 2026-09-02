"""Regime confirmation and targeted choppy-market guard for AUTO scans.

Adds DMI, RSI momentum, price structure and a simple market-regime read to each
completed-candle scan. Normal confirmation remains score-based. A narrow hard
guard is used only when low ADX combines with EMA compression, VWAP congestion
or repeated candle flips. A score-88, real-MTF, two-candle range breakout is
still allowed so early directional moves are not missed.
"""
from __future__ import annotations

import math

from bot import auto_portfolio_runtime as runtime

VERSION = "OKAI-REGIME-ACCURACY-CONFIRMATION-V1"
CHOPPY_ADX_MAX = 22.0
CHOPPY_EMA_SPREAD_ATR_MAX = 0.25
CHOPPY_VWAP_DISTANCE_ATR_MAX = 0.35
CHOPPY_MIN_BODY_FLIPS = 3
CHOPPY_VWAP_CROSS_WINDOW = 45
CHOPPY_VWAP_CROSS_MIN = 3
VERY_WEAK_ADX_MAX = 18.0
CHOPPY_BREAKOUT_BUFFER_ATR = 0.08
CHOPPY_BREAKOUT_MIN_SCORE = 88
CHOPPY_BLOCK_REASON = "CHOPPY_RANGE_NO_BREAKOUT"


def _f(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except Exception:
        return float(default)


def _series(df, name):
    try:
        return df[name].astype(float)
    except Exception:
        return None


def _rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(period).mean()
    loss = (-delta.clip(upper=0.0)).rolling(period).mean()
    rs = gain / loss.replace(0.0, float("nan"))
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(50.0)


def _dmi(df, period=14):
    high = _series(df, "high")
    low = _series(df, "low")
    close = _series(df, "close")
    if high is None or low is None or close is None:
        return None, None

    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0.0), 0.0)
    minus_dm = down.where((down > up) & (down > 0.0), 0.0)
    tr = (high - low).to_frame("hl")
    tr["hc"] = (high - close.shift()).abs()
    tr["lc"] = (low - close.shift()).abs()
    atr = tr.max(axis=1).rolling(period).mean().replace(0.0, float("nan"))
    plus_di = 100.0 * plus_dm.rolling(period).mean() / atr
    minus_di = 100.0 * minus_dm.rolling(period).mean() / atr
    return plus_di.fillna(0.0), minus_di.fillna(0.0)


def _structure(df):
    try:
        rows = df.iloc[-6:-1]
        if len(rows) < 4:
            return "NEUTRAL"
        highs = [float(v) for v in rows["high"]]
        lows = [float(v) for v in rows["low"]]
        bull = highs[-1] > highs[-2] and lows[-1] > lows[-2]
        bear = highs[-1] < highs[-2] and lows[-1] < lows[-2]
        if bull:
            return "BULLISH"
        if bear:
            return "BEARISH"
    except Exception:
        pass
    return "NEUTRAL"


def _choppy_assessment(df, market, candidate, score, minimum):
    adx = _f(market.get("adx"), 0.0)
    atr = max(0.01, _f(market.get("atr"), 0.01))
    price = _f(market.get("price"), 0.0)
    ema9 = _f(market.get("ema9"), price)
    ema21 = _f(market.get("ema21"), price)
    vwap = _f(market.get("vwap"), price)
    vwap_reliable = not bool(market.get("vwap_fallback_used", False))

    weak_adx = 0.0 < adx < CHOPPY_ADX_MAX
    very_weak_adx = 0.0 < adx < VERY_WEAK_ADX_MAX
    ema_compressed = abs(ema9 - ema21) / atr <= CHOPPY_EMA_SPREAD_ATR_MAX
    near_vwap = bool(
        vwap_reliable
        and abs(price - vwap) / atr <= CHOPPY_VWAP_DISTANCE_ATR_MAX
    )

    body_flips = 0
    vwap_crosses = 0
    breakout = False
    two_candle_momentum = False
    try:
        completed = df.iloc[-8:-1]
        directions = []
        for _, row in completed.iterrows():
            body = _f(row.get("close")) - _f(row.get("open"))
            directions.append(1 if body > 0 else -1 if body < 0 else 0)
        nonzero = [value for value in directions if value]
        body_flips = sum(
            1 for left, right in zip(nonzero, nonzero[1:]) if left != right
        )

        latest = df.iloc[-2]
        previous = df.iloc[-3]
        prior_range = df.iloc[-8:-2]
        latest_close = _f(latest.get("close"), price)
        if candidate == "CE":
            breakout = latest_close >= (
                max(_f(value) for value in prior_range["high"])
                + CHOPPY_BREAKOUT_BUFFER_ATR * atr
            )
            two_candle_momentum = bool(
                _f(previous.get("close")) > _f(previous.get("open"))
                and latest_close > _f(latest.get("open"))
            )
        elif candidate == "PE":
            breakout = latest_close <= (
                min(_f(value) for value in prior_range["low"])
                - CHOPPY_BREAKOUT_BUFFER_ATR * atr
            )
            two_candle_momentum = bool(
                _f(previous.get("close")) < _f(previous.get("open"))
                and latest_close < _f(latest.get("open"))
            )
    except Exception:
        body_flips = 0
        breakout = False
        two_candle_momentum = False

    try:
        completed = df.iloc[-(CHOPPY_VWAP_CROSS_WINDOW + 1):-1]
        if "VWAP" in completed.columns and len(completed) >= 4:
            # Ignore tiny touches inside a two-percent ATR dead-band.  Only a
            # real close from one side of VWAP to the other counts as a cross.
            band = max(0.01, 0.02 * atr)
            sides = []
            for _, row in completed.iterrows():
                distance = _f(row.get("close")) - _f(row.get("VWAP"))
                side = 1 if distance > band else -1 if distance < -band else 0
                if side:
                    sides.append(side)
            vwap_crosses = sum(
                1 for left, right in zip(sides, sides[1:]) if left != right
            )
    except Exception:
        vwap_crosses = 0

    repeated_flips = body_flips >= CHOPPY_MIN_BODY_FLIPS
    repeated_vwap_crosses = vwap_crosses >= CHOPPY_VWAP_CROSS_MIN
    congestion_votes = sum((ema_compressed, near_vwap, repeated_flips))
    # ADX below 18 or three genuine VWAP crosses in the last 45 completed
    # minutes is already a no-trade range signal.  The older two-vote test is
    # retained for transition regimes between ADX 18 and 22.
    choppy = bool(
        very_weak_adx
        or repeated_vwap_crosses
        or (weak_adx and congestion_votes >= 2)
    )
    strong_breakout = bool(
        candidate in {"CE", "PE"}
        and score >= max(minimum, CHOPPY_BREAKOUT_MIN_SCORE)
        and market.get("mtf_confirmed", False)
        and breakout
        and two_candle_momentum
    )
    # The strong-breakout escape hatch is intentionally disabled for the two
    # clearest whipsaw regimes.  A low-ADX/VWAP-crossing market must first
    # leave the range and rebuild ADX before AUTO can enter again.
    strong_breakout = bool(
        strong_breakout
        and not very_weak_adx
        and not repeated_vwap_crosses
    )
    return {
        "choppy": choppy,
        "hard_block": bool(choppy and not strong_breakout),
        "strong_breakout_override": strong_breakout,
        "weak_adx": weak_adx,
        "very_weak_adx": very_weak_adx,
        "ema_compressed": ema_compressed,
        "near_vwap": near_vwap,
        "repeated_body_flips": repeated_flips,
        "body_flips": body_flips,
        "vwap_crosses_45m": vwap_crosses,
        "repeated_vwap_crosses": repeated_vwap_crosses,
        "congestion_votes": congestion_votes,
        "breakout": breakout,
        "two_candle_momentum": two_candle_momentum,
    }


def _confirmation(df, scan):
    signal = dict(scan.get("signal_data") or {})
    market = dict(scan.get("market_data") or {})
    candidate = str(signal.get("candidate_signal") or signal.get("signal") or "WAIT").upper()
    if candidate not in {"CE", "PE"}:
        return {"version": VERSION, "adjustment": 0, "regime": "NO_DIRECTION"}

    close = _series(df, "close")
    if close is None or len(close) < 20:
        return {"version": VERSION, "adjustment": 0, "regime": "INSUFFICIENT_DATA"}

    plus_di, minus_di = _dmi(df)
    rsi = _rsi(close)
    idx = -2  # last completed candle, same convention as AUTO runtime
    prev = -3
    pdi = _f(plus_di.iloc[idx] if plus_di is not None else 0.0)
    mdi = _f(minus_di.iloc[idx] if minus_di is not None else 0.0)
    rsi_now = _f(rsi.iloc[idx], 50.0)
    rsi_prev = _f(rsi.iloc[prev], 50.0)
    adx = _f(market.get("adx"), 0.0)
    price = _f(market.get("price"), _f(close.iloc[idx]))
    vwap = _f(market.get("vwap"), price)
    structure = _structure(df)

    bullish = candidate == "CE"
    dmi_ok = pdi > mdi if bullish else mdi > pdi
    dmi_strong_against = (mdi - pdi >= 8.0) if bullish else (pdi - mdi >= 8.0)
    rsi_ok = (rsi_now > 50.0 and rsi_now >= rsi_prev) if bullish else (rsi_now < 50.0 and rsi_now <= rsi_prev)
    rsi_against = (rsi_now < 45.0 and rsi_now < rsi_prev) if bullish else (rsi_now > 55.0 and rsi_now > rsi_prev)
    vwap_ok = price >= vwap if bullish else price <= vwap
    structure_ok = structure == ("BULLISH" if bullish else "BEARISH")
    structure_against = structure == ("BEARISH" if bullish else "BULLISH")

    if adx >= 25.0:
        regime = "TRENDING"
    elif adx <= 18.0:
        regime = "RANGE"
    else:
        regime = "TRANSITION"

    adjustment = 0
    reasons = []
    if dmi_ok:
        adjustment += 3
        reasons.append("DMI_CONFIRM")
    elif dmi_strong_against:
        adjustment -= 4
        reasons.append("DMI_STRONG_OPPOSITE")

    if rsi_ok:
        adjustment += 2
        reasons.append("RSI_MOMENTUM_CONFIRM")
    elif rsi_against:
        adjustment -= 3
        reasons.append("RSI_MOMENTUM_OPPOSITE")

    if structure_ok:
        adjustment += 3
        reasons.append("STRUCTURE_CONFIRM")
    elif structure_against:
        adjustment -= 4
        reasons.append("STRUCTURE_OPPOSITE")

    # VWAP is intentionally light-touch because the existing engine already uses
    # it; this layer only records alignment and gives one small confirmation.
    if vwap_ok:
        adjustment += 1
        reasons.append("VWAP_ALIGNED")

    # In a weak/range regime do not hard-block breakouts. Only remove a little
    # confidence when neither structure nor DMI confirms the proposed direction.
    if regime == "RANGE" and not dmi_ok and not structure_ok:
        adjustment -= 2
        reasons.append("RANGE_WEAK_CONFIRMATION")

    adjustment = max(-8, min(7, int(adjustment)))
    minimum = int(round(_f(signal.get("min_score", 82), 82.0)))
    chop = _choppy_assessment(
        df,
        market,
        candidate,
        max(0, min(100, int(round(_f(signal.get("score"), 0))) + adjustment)),
        minimum,
    )
    if chop["choppy"]:
        regime = "CHOPPY_RANGE"
        reasons.append(
            "CHOPPY_BREAKOUT_OVERRIDE"
            if chop["strong_breakout_override"]
            else CHOPPY_BLOCK_REASON
        )
    return {
        "version": VERSION,
        "candidate": candidate,
        "regime": regime,
        "adjustment": adjustment,
        "adx": round(adx, 2),
        "plus_di": round(pdi, 2),
        "minus_di": round(mdi, 2),
        "rsi": round(rsi_now, 2),
        "rsi_rising": bool(rsi_now >= rsi_prev),
        "structure": structure,
        "vwap_aligned": bool(vwap_ok),
        "reasons": reasons,
        "hard_block": chop["hard_block"],
        "choppy_guard": chop,
    }


def apply_regime_accuracy_confirmation_patch() -> None:
    if getattr(runtime, "_okai_regime_accuracy_confirmation_v1", False):
        return

    original_build_scan = runtime._build_scan

    def build_scan_with_accuracy_confirmation(user_id, underlying, df, profile, loss_streak):
        scan = original_build_scan(user_id, underlying, df, profile, loss_streak)
        try:
            if not isinstance(scan, dict) or scan.get("status") != "OK":
                return scan
            signal = scan.get("signal_data")
            market = scan.get("market_data")
            if not isinstance(signal, dict) or not isinstance(market, dict):
                return scan

            layer = _confirmation(df, scan)
            candidate = str(
                layer.get("candidate")
                or signal.get("candidate_signal")
                or signal.get("signal")
                or "WAIT"
            ).upper()
            adjustment = int(layer.get("adjustment", 0) or 0)
            old_score = int(round(_f(signal.get("score"), 0.0)))
            min_score = int(round(_f(signal.get("min_score", signal.get("min_score_required", 82)), 82.0)))
            new_score = max(0, min(100, old_score + adjustment))

            signal["pre_accuracy_score"] = old_score
            signal["accuracy_confirmation"] = layer
            signal["accuracy_adjustment"] = adjustment
            signal["score"] = new_score
            signal["regime"] = layer.get("regime")

            # The final guard is installed after real 5-minute MTF and pullback
            # wrappers. This early layer records the assessment only.
            signal["choppy_market_detected"] = bool(
                (layer.get("choppy_guard") or {}).get("choppy", False)
            )

            # Confirmation is score-based except for the targeted choppy guard.
            # It never turns an originally blocked setup into an allowed trade.
            was_allowed = bool(signal.get("trade_allowed", False))
            if was_allowed and new_score < min_score:
                signal["trade_allowed"] = False
                signal["signal"] = "WAIT"
                signal.setdefault("warnings", []).append(
                    f"Accuracy confirmation {adjustment:+d}: score {new_score}<{min_score}"
                )
            elif was_allowed:
                signal.setdefault("warnings", []).append(
                    f"Accuracy confirmation {adjustment:+d} ({layer.get('regime')})"
                )

            market["regime"] = layer.get("regime")
            market["accuracy_confirmation"] = layer
            market["signal_score"] = new_score
            market["choppy_guard"] = layer.get("choppy_guard")
        except Exception as exc:
            try:
                signal = scan.get("signal_data") if isinstance(scan, dict) else None
                if isinstance(signal, dict):
                    signal["accuracy_confirmation_error"] = str(exc)[:160]
            except Exception:
                pass
        return scan

    runtime._build_scan = build_scan_with_accuracy_confirmation
    runtime._okai_regime_accuracy_confirmation_v1 = True
