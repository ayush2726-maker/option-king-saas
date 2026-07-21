"""Structural Exit V2.

Entry momentum is not an exit rule.  An open bought-option position exits through:
- real option-premium ATR stop / dynamic profit lock (checked every runtime loop),
- one completed index candle where VWAP, Supertrend and EMA9/EMA21 all flip
  opposite to the position,
- or the existing 15:25 EOD exit.

A partial one/two-indicator deterioration does not force an exit.  When the
position is already profitable it tightens the stop to at least breakeven.
"""

import inspect

from backtest import routes as backtest_routes
from bot import auto_portfolio_runtime as runtime
from bot import dynamic_exit


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


def _detect_structural_reversal_v2(
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
        "exit_rule": "VWAP_SUPERTREND_EMA_TRIPLE_FLIP",
        **state,
    }


def _runtime_evaluate_exit_v2(trade, ltp, market_data, candle_id):
    entry = _f(trade["entry_price"])
    old_sl = _f(runtime._v(trade, "sl_price", max(0.05, entry - 0.05)))
    risk = _f(runtime._v(trade, "initial_risk", max(0.05, entry - old_sl)))
    risk = max(0.05, risk)
    peak = _f(runtime._v(trade, "peak_price", entry))
    updates = runtime._i(runtime._v(trade, "trail_updates", 0))

    trail = runtime._legacy()._dynamic_profit_lock(
        entry, risk, old_sl, peak, ltp
    )
    if trail["updated"]:
        updates += 1

    structure = structural_flip(runtime._v(trade, "side", ""), market_data)

    # Do not exit on one noisy indicator. If two indicators deteriorate while
    # the option is profitable, prevent a winner from becoming a loser.
    if structure["opposite_count"] >= 2 and ltp >= entry:
        tightened = max(_f(trail["sl_price"]), entry)
        if tightened > _f(trail["sl_price"]):
            trail["sl_price"] = round(min(tightened, max(0.05, ltp - 0.05)), 2)
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
    elif structure["all_three_flipped"]:
        reason = "VWAP + SUPERTREND + EMA STRUCTURAL EXIT"
    elif eod:
        reason = "EOD EXIT 15:25 IST"
    else:
        reason = None

    return {
        "trail": trail,
        "risk": risk,
        "updates": updates,
        "reversal": {
            "exit": bool(structure["all_three_flipped"]),
            "count": 1 if structure["all_three_flipped"] else 0,
            "last_candle": candle_id,
            "mode": "VWAP_ST_EMA_TRIPLE_FLIP_V2",
            **structure,
        },
        "reason": reason,
    }


def _patch_backtest_one_candle_exit():
    """Change only the caller confirmation count/reason, keeping its engine intact."""
    try:
        source = inspect.getsource(backtest_routes.run_realistic_day_backtest)
        changed = source.replace(
            "reversal_required_candles = 2",
            "reversal_required_candles = 1",
        ).replace(
            '"TWO_CANDLE_REVERSAL_EXIT"',
            '"VWAP_ST_EMA_STRUCTURAL_EXIT"',
        ).replace(
            '"mode": "CONFIRMED_TWO_CANDLE"',
            '"mode": "VWAP_ST_EMA_TRIPLE_FLIP"',
        ).replace(
            '"confirmation_candles": 2',
            '"confirmation_candles": 1',
        )
        if changed != source:
            exec(compile(changed, backtest_routes.__file__, "exec"), backtest_routes.__dict__)
            # AUTO portfolio calls the captured single-index function directly.
            # Repoint it so Daily, AUTO and Monthly all use the same exit engine.
            backtest_routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST = (
                backtest_routes.run_realistic_day_backtest
            )
            return True
    except Exception:
        pass
    return False


def apply_structural_exit_v2_patch():
    if getattr(runtime, "_okai_structural_exit_v2", False):
        return

    dynamic_exit.detect_structural_reversal = _detect_structural_reversal_v2
    backtest_routes.detect_structural_reversal = _detect_structural_reversal_v2
    runtime._evaluate_exit = _runtime_evaluate_exit_v2
    patched_backtest = _patch_backtest_one_candle_exit()

    runtime._okai_structural_exit_v2 = True
    runtime._okai_structural_exit_backtest_patched = bool(patched_backtest)
