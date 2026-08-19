"""Soft accuracy confirmation layer for AUTO Portfolio scans.

Adds DMI, RSI momentum, price structure and a simple market-regime read to each
completed-candle scan.  This patch is deliberately non-blocking: it adjusts the
existing decision score for confirmation/contradiction but never introduces a
new hard veto.  Existing expiry, momentum, broker, option-quality, risk and
order-safety rules remain authoritative.
"""
from __future__ import annotations

import math

from bot import auto_portfolio_runtime as runtime

VERSION = "OKAI-REGIME-ACCURACY-CONFIRMATION-V1"


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
        "hard_block": False,
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
            adjustment = int(layer.get("adjustment", 0) or 0)
            old_score = int(round(_f(signal.get("score"), 0.0)))
            min_score = int(round(_f(signal.get("min_score", signal.get("min_score_required", 82)), 82.0)))
            new_score = max(0, min(100, old_score + adjustment))

            signal["pre_accuracy_score"] = old_score
            signal["accuracy_confirmation"] = layer
            signal["accuracy_adjustment"] = adjustment
            signal["score"] = new_score
            signal["regime"] = layer.get("regime")

            # This layer is score-only. Preserve all existing hard gates, but if
            # the original setup was score-qualified, reflect the adjusted score
            # against the same threshold. It never turns an originally blocked
            # setup into an allowed trade.
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
