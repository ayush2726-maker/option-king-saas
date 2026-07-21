"""Adaptive Structural Exit V3.

This module fixes the real captured single-index backtest function rather than
patching the public AUTO wrapper. Bought-option exits use:

- option-premium ATR stop and dynamic profit lock on every runtime loop,
- VWAP + Supertrend + EMA9/EMA21 all flipping opposite on completed index candles,
- adaptive confirmation: immediate at -0.35R or worse, otherwise two completed
  structural-flip candles,
- and the existing 15:25 EOD exit.

Two-candle colour momentum is an ENTRY trigger only and is never an exit rule.
"""

import inspect

from backtest import routes as backtest_routes
from bot import auto_portfolio_runtime as runtime
from bot import dynamic_exit


STRUCTURAL_LOSS_FAST_EXIT_R = -0.35
STRUCTURAL_NORMAL_CONFIRM_CANDLES = 2


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def structural_flip(position_side, market_data):
    market = market_data or {}
    side = str(position_side or "").upper()
    price = _f(market.get("price"), 0)
    vwap = _f(market.get("vwap"), price)
    ema9 = _f(market.get("ema9"), price)
    ema21 = _f(market.get("ema21"), ema9)
    st = str(market.get("supertrend_dir") or "NEUTRAL").upper()

    if side == "CE":
        checks = {
            "vwap_opposite": price < vwap,
            "supertrend_opposite": st == "DOWN",
            "ema_opposite": ema9 < ema21,
        }
    elif side == "PE":
        checks = {
            "vwap_opposite": price > vwap,
            "supertrend_opposite": st == "UP",
            "ema_opposite": ema9 > ema21,
        }
    else:
        checks = {
            "vwap_opposite": False,
            "supertrend_opposite": False,
            "ema_opposite": False,
        }

    count = sum(1 for passed in checks.values() if passed)
    return {
        **checks,
        "opposite_count": count,
        "all_three_flipped": count == 3,
        "price": round(price, 2),
        "vwap": round(vwap, 2),
        "ema9": round(ema9, 2),
        "ema21": round(ema21, 2),
        "supertrend_dir": st,
        "side": side,
    }


def _detect_structural_reversal_v3(
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
    state = structural_flip(
        position_side,
        {
            "price": price,
            "vwap": vwap,
            "ema9": ema9,
            "ema21": ema21,
            "supertrend_dir": supertrend_dir,
        },
    )
    return {
        "detected": bool(state["all_three_flipped"]),
        "side": state["side"],
        "vwap_broken": state["vwap_opposite"],
        "ema9_broken": state["ema_opposite"],
        "opposite_confirmed": state["all_three_flipped"],
        "trend_flip_confirmed": bool(
            state["supertrend_opposite"] and state["ema_opposite"]
        ),
        "valid_opposite_signal": False,
        "opposite_signal": str(opposite_signal or "WAIT").upper(),
        "opposite_score": round(_f(opposite_score), 2),
        "min_score": round(_f(min_score, 82), 2),
        "exit_rule": "VWAP_SUPERTREND_EMA_ADAPTIVE_V3",
        **state,
    }


def _runtime_evaluate_exit_v3(trade, ltp, market_data, candle_id):
    entry = _f(trade["entry_price"])
    old_sl = _f(runtime._v(trade, "sl_price", max(0.05, entry - 0.05)))
    risk = max(
        0.05,
        _f(runtime._v(trade, "initial_risk", max(0.05, entry - old_sl))),
    )
    peak = _f(runtime._v(trade, "peak_price", entry))
    updates = runtime._i(runtime._v(trade, "trail_updates", 0))

    trail = runtime._legacy()._dynamic_profit_lock(
        entry, risk, old_sl, peak, ltp
    )
    if trail["updated"]:
        updates += 1

    structure = structural_flip(runtime._v(trade, "side", ""), market_data)
    current_r = (ltp - entry) / risk

    previous_count = runtime._i(runtime._v(trade, "reversal_count", 0))
    previous_candle = str(runtime._v(trade, "reversal_last_candle", "") or "")
    current_candle = str(candle_id or "")

    # The runtime checks option LTP every second. Count structural confirmation
    # only once per completed index candle, never once per one-second tick.
    if current_candle and current_candle != previous_candle:
        reversal_count = previous_count + 1 if structure["all_three_flipped"] else 0
        reversal_last_candle = current_candle
    else:
        reversal_count = previous_count
        reversal_last_candle = previous_candle or current_candle

    required = (
        1
        if current_r <= STRUCTURAL_LOSS_FAST_EXIT_R
        else STRUCTURAL_NORMAL_CONFIRM_CANDLES
    )
    structural_exit = bool(
        structure["all_three_flipped"] and reversal_count >= required
    )

    # Two-of-three weakness is not an exit. Protect an existing winner instead.
    if structure["opposite_count"] >= 2 and ltp >= entry:
        tightened = max(_f(trail["sl_price"]), entry)
        if tightened > _f(trail["sl_price"]):
            trail["sl_price"] = round(
                min(tightened, max(0.05, ltp - 0.05)), 2
            )
            trail["stage"] = "STRUCTURE_BREAKEVEN_TIGHTEN"
            trail["updated"] = True
            updates += 1

    eod = runtime._now_ist().hour * 60 + runtime._now_ist().minute >= 15 * 60 + 25
    hit = ltp <= _f(trail["sl_price"])

    if hit and _f(trail["sl_price"]) >= entry:
        reason = (
            "PROFIT LOCK TRAIL HIT"
            f" | {trail['stage']} | locked={trail.get('locked_r', 0)}R"
        )
    elif hit:
        reason = "PURE ATR SL HIT"
    elif structural_exit:
        reason = (
            "VWAP + SUPERTREND + EMA STRUCTURAL EXIT"
            f" | confirmations={reversal_count}/{required}"
            f" | current_r={current_r:.2f}"
        )
    elif eod:
        reason = "EOD EXIT 15:25 IST"
    else:
        reason = None

    return {
        "trail": trail,
        "risk": risk,
        "updates": updates,
        "reversal": {
            "exit": structural_exit,
            "count": reversal_count,
            "last_candle": reversal_last_candle,
            "required": required,
            "current_r": round(current_r, 2),
            "mode": "VWAP_ST_EMA_ADAPTIVE_V3",
            **structure,
        },
        "reason": reason,
    }


def _patch_captured_single_index_backtest():
    """Compile and install the actual captured single-index backtest function."""
    target = getattr(
        backtest_routes,
        "_OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST",
        None,
    )
    if target is None:
        return False, "CAPTURED_SINGLE_INDEX_FUNCTION_MISSING"

    try:
        source = inspect.getsource(target)
        changed = source

        # Avoid overwriting the public AUTO/single wrapper when executing source.
        changed = changed.replace(
            "def run_realistic_day_backtest(",
            "def _okai_single_index_adaptive_exit_v3(",
            1,
        )
        changed = changed.replace(
            "reversal_required_candles = 2",
            "reversal_required_candles = (\n"
            "                1 if current_r <= -0.35 else 2\n"
            "            )",
        )
        changed = changed.replace(
            '"TWO_CANDLE_REVERSAL_EXIT"',
            '"VWAP_ST_EMA_STRUCTURAL_EXIT"',
        )
        changed = changed.replace(
            '"mode": "CONFIRMED_TWO_CANDLE"',
            '"mode": "VWAP_ST_EMA_ADAPTIVE_V3"',
        )
        changed = changed.replace(
            '"confirmation_candles": 2',
            '"confirmation_candles": "1_AT_MINUS_0.35R_ELSE_2"',
        )

        if changed == source:
            return False, "SOURCE_TRANSFORM_NO_MATCH"

        exec(
            compile(changed, backtest_routes.__file__, "exec"),
            backtest_routes.__dict__,
        )
        patched = backtest_routes.__dict__.get(
            "_okai_single_index_adaptive_exit_v3"
        )
        if not callable(patched):
            return False, "PATCHED_FUNCTION_NOT_CREATED"

        backtest_routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST = patched
        try:
            backtest_routes._OKAI_BACKTEST_CANDLE_CACHE.clear()
        except Exception:
            pass
        return True, "OK"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}:{str(exc)[:180]}"


def apply_structural_exit_v2_patch():
    # Preserve old import/function name used by main.py, but install V3 once.
    if getattr(runtime, "_okai_structural_exit_v3", False):
        return

    dynamic_exit.detect_structural_reversal = _detect_structural_reversal_v3
    backtest_routes.detect_structural_reversal = _detect_structural_reversal_v3
    runtime._evaluate_exit = _runtime_evaluate_exit_v3

    patched, diagnostic = _patch_captured_single_index_backtest()
    runtime._okai_structural_exit_v2 = True
    runtime._okai_structural_exit_v3 = True
    runtime._okai_structural_exit_backtest_patched = bool(patched)
    runtime._okai_structural_exit_backtest_diagnostic = diagnostic
