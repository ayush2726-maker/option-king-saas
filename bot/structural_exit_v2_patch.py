"""Adaptive Structural Exit and Balanced Premium Path V5.

Bought-option exits use:
- option-premium ATR stop and the active profit-lock helper on every loop;
- VWAP + Supertrend + EMA9/EMA21 all flipping opposite on completed candles;
- adaptive structural confirmation: immediate at -0.35R or worse, otherwise two
  completed structural-flip candles;
- a no-follow-through exit after three completed candles when the trade never
  reached +0.30R and at least two of VWAP/Supertrend/EMA have turned opposite;
- and the existing 15:25 EOD exit.

The captured Daily/AUTO/Monthly backtest uses spot POINT movement multiplied by
the same option response used by the ATR engine: 0.50 normal and 1.00 expiry.
It also applies a ten-candle cooldown after a losing trade.
"""

from datetime import datetime, timezone
import inspect

from backtest import routes as backtest_routes
from bot import auto_portfolio_runtime as runtime
from bot import dynamic_exit


STRUCTURAL_LOSS_FAST_EXIT_R = -0.35
STRUCTURAL_NORMAL_CONFIRM_CANDLES = 2
NORMAL_OPTION_POINT_RESPONSE = 0.50
EXPIRY_OPTION_POINT_RESPONSE = 1.00
NO_FOLLOW_THROUGH_CANDLES = 3
NO_FOLLOW_THROUGH_MAX_PEAK_R = 0.30


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


def _parse_dt(value):
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def _bars_held(trade, candle_id):
    entry_dt = _parse_dt(runtime._v(trade, "created_at", None))
    candle_dt = _parse_dt(candle_id)
    if entry_dt is None or candle_dt is None:
        return 0
    return int(max(0.0, (candle_dt - entry_dt).total_seconds()) // 60)


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


def _detect_structural_reversal_v5(
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
        "exit_rule": "VWAP_SUPERTREND_EMA_BALANCED_V5",
        **state,
    }


def _runtime_evaluate_exit_v5(trade, ltp, market_data, candle_id):
    entry = _f(trade["entry_price"])
    old_sl = _f(runtime._v(trade, "sl_price", max(0.05, entry - 0.05)))
    risk = max(
        0.05,
        _f(runtime._v(trade, "initial_risk", max(0.05, entry - old_sl))),
    )
    peak = _f(runtime._v(trade, "peak_price", entry))
    updates = runtime._i(runtime._v(trade, "trail_updates", 0))

    trail = runtime._legacy()._dynamic_profit_lock(
        entry,
        risk,
        old_sl,
        peak,
        ltp,
    )
    if trail["updated"]:
        updates += 1

    structure = structural_flip(runtime._v(trade, "side", ""), market_data)
    current_r = (ltp - entry) / risk
    peak_r = max(
        _f(trail.get("peak_r"), 0),
        (max(entry, peak, ltp) - entry) / risk,
    )

    previous_count = runtime._i(runtime._v(trade, "reversal_count", 0))
    previous_candle = str(runtime._v(trade, "reversal_last_candle", "") or "")
    current_candle = str(candle_id or "")

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

    bars_held = _bars_held(trade, candle_id)
    no_follow_through = bool(
        bars_held >= NO_FOLLOW_THROUGH_CANDLES
        and peak_r < NO_FOLLOW_THROUGH_MAX_PEAK_R
        and structure["opposite_count"] >= 2
    )

    eod = runtime._now_ist().hour * 60 + runtime._now_ist().minute >= 15 * 60 + 25
    hit = ltp <= _f(trail["sl_price"])

    if hit and _f(trail["sl_price"]) >= entry:
        reason = (
            "PROFIT LOCK TRAIL HIT"
            f" | {trail['stage']} | locked={trail.get('locked_r', 0)}R"
        )
    elif hit:
        reason = "PURE ATR SL HIT"
    elif no_follow_through:
        reason = (
            "NO FOLLOW THROUGH EXIT"
            f" | bars={bars_held}"
            f" | peak={peak_r:.2f}R"
            f" | weakness={structure['opposite_count']}/3"
        )
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
            "mode": "VWAP_ST_EMA_ADAPTIVE_BALANCED_V5",
            **structure,
        },
        "no_follow_through": {
            "exit": no_follow_through,
            "bars_held": bars_held,
            "peak_r": round(peak_r, 2),
            "max_peak_r": NO_FOLLOW_THROUGH_MAX_PEAK_R,
            "weakness_count": structure["opposite_count"],
            "required_weakness": 2,
        },
        "reason": reason,
    }


def _patch_captured_single_index_backtest():
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
        changed = changed.replace(
            "def run_realistic_day_backtest(",
            "def _okai_single_index_balanced_exit_v5(",
            1,
        )
        changed = changed.replace(
            "def _okai_single_index_premium_path_v4(",
            "def _okai_single_index_balanced_exit_v5(",
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
            '"mode": "VWAP_ST_EMA_ADAPTIVE_BALANCED_V5"',
        )
        changed = changed.replace(
            '"mode": "VWAP_ST_EMA_ADAPTIVE_V4"',
            '"mode": "VWAP_ST_EMA_ADAPTIVE_BALANCED_V5"',
        )
        changed = changed.replace(
            '"confirmation_candles": 2',
            '"confirmation_candles": "1_AT_MINUS_0.35R_ELSE_2"',
        )

        old_premium_block = '''            close_pct = (spot_close-entry_spot)/entry_spot*100
            if side == "CE":
                good_pct = (spot_high-entry_spot)/entry_spot*100
                bad_pct = (spot_low-entry_spot)/entry_spot*100
            else:
                close_pct = -close_pct
                good_pct = (entry_spot-spot_low)/entry_spot*100
                bad_pct = (entry_spot-spot_high)/entry_spot*100

            response = 8.0
            current_premium = max(0.5, entry*(1+close_pct*response/100))
            premium_high = max(0.5, entry*(1+good_pct*response/100))
            premium_low = max(0.5, entry*(1+bad_pct*response/100))'''
        new_premium_block = '''            # Synthetic option path uses spot POINT movement.
            if side == "CE":
                close_points = spot_close - entry_spot
                favorable_points = spot_high - entry_spot
                adverse_points = spot_low - entry_spot
            else:
                close_points = entry_spot - spot_close
                favorable_points = entry_spot - spot_low
                adverse_points = entry_spot - spot_high

            response = 1.0 if is_expiry_day else 0.5
            current_premium = max(0.5, entry + close_points * response)
            premium_high = max(0.5, entry + favorable_points * response)
            premium_low = max(0.5, entry + adverse_points * response)'''
        changed = changed.replace(old_premium_block, new_premium_block)

        if "last_loss_exit_index = None" not in changed:
            changed = changed.replace(
                "    consecutive_losses = 0\n",
                "    consecutive_losses = 0\n"
                "    last_loss_exit_index = None\n",
                1,
            )
        if "last_loss_exit_index = i if pnl < 0 else None" not in changed:
            changed = changed.replace(
                "                consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0\n",
                "                consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0\n"
                "                last_loss_exit_index = i if pnl < 0 else None\n",
            )

        entry_gate = '''        if (
            signal_data["trade_allowed"]
            and signal_data["signal"] in ("CE", "PE")
            and signal_data["score"] >= entry_threshold
            and core_quality_ok
        ):'''
        balanced_entry_gate = '''        loss_cooldown_active = (
            last_loss_exit_index is not None
            and i - last_loss_exit_index < 10
        )

        if (
            signal_data["trade_allowed"]
            and signal_data["signal"] in ("CE", "PE")
            and signal_data["score"] >= entry_threshold
            and core_quality_ok
            and not loss_cooldown_active
        ):'''
        if "loss_cooldown_active" not in changed:
            changed = changed.replace(entry_gate, balanced_entry_gate)

        if '"entry_index": i' not in changed:
            changed = changed.replace(
                '                "entry_time": str(last["time"]),\n',
                '                "entry_time": str(last["time"]),\n'
                '                "entry_index": i,\n',
            )

        structural_block = '''            structural_exit = (
                open_trade["reversal_count"]
                >= reversal_required_candles
            )

            if ('''
        balanced_structural_block = '''            structural_exit = (
                open_trade["reversal_count"]
                >= reversal_required_candles
            )
            bars_held = max(
                0,
                i - int(open_trade.get("entry_index", i)),
            )
            peak_r_seen = max(
                float(open_trade.get("peak_r", 0) or 0),
                (premium_high - entry) / risk_points,
            )
            no_follow_through_exit = (
                bars_held >= 3
                and peak_r_seen < 0.30
                and int(reversal.get("opposite_count", 0) or 0) >= 2
            )

            if ('''
        if "no_follow_through_exit" not in changed:
            changed = changed.replace(structural_block, balanced_structural_block)
            changed = changed.replace(
                "                hit_sl\n                or force_combined_reserve_exit",
                "                hit_sl\n                or no_follow_through_exit\n                or force_combined_reserve_exit",
            )
            changed = changed.replace(
                '''                elif force_combined_reserve_exit:
                    exit_price = round(''',
                '''                elif no_follow_through_exit:
                    exit_price = round(
                        current_premium,
                        2,
                    )
                    reason = "NO_FOLLOW_THROUGH_EXIT_3C_2OF3"
                elif force_combined_reserve_exit:
                    exit_price = round(''',
            )
            changed = changed.replace(
                '                    "fixed_target_enabled": False,',
                '                    "no_follow_through_exit": bool(no_follow_through_exit),\n'
                '                    "bars_held_at_exit": bars_held,\n'
                '                    "peak_r_seen": round(peak_r_seen, 2),\n'
                '                    "fixed_target_enabled": False,',
            )

        changed = changed.replace(
            '"premium_model": "SPOT_POINTS_X_OPTION_RESPONSE_V4"',
            '"premium_model": "SPOT_POINTS_X_OPTION_RESPONSE_BALANCED_V5"',
        )
        changed = changed.replace(
            '"premium_response_factor": response,',
            '"premium_response_factor": response,\n'
            '                    "premium_model": "SPOT_POINTS_X_OPTION_RESPONSE_BALANCED_V5",',
            1,
        ) if "premium_model" not in changed else changed

        required = (
            "_okai_single_index_balanced_exit_v5",
            "NO_FOLLOW_THROUGH_EXIT_3C_2OF3",
            "loss_cooldown_active",
            "SPOT_POINTS_X_OPTION_RESPONSE_BALANCED_V5",
        )
        missing = [marker for marker in required if marker not in changed]
        if missing:
            return False, "SOURCE_TRANSFORM_MISSING:" + ",".join(missing)
        if "response = 8.0" in changed:
            return False, "LEGACY_PERCENT_RESPONSE_STILL_PRESENT"

        exec(
            compile(changed, backtest_routes.__file__, "exec"),
            backtest_routes.__dict__,
        )
        patched = backtest_routes.__dict__.get(
            "_okai_single_index_balanced_exit_v5"
        )
        if not callable(patched):
            return False, "PATCHED_FUNCTION_NOT_CREATED"

        backtest_routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST = patched
        try:
            backtest_routes._OKAI_BACKTEST_CANDLE_CACHE.clear()
        except Exception:
            pass
        return True, "OK_BALANCED_NO_FOLLOW_THROUGH_V5"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}:{str(exc)[:180]}"


def apply_structural_exit_v2_patch():
    # Preserve the historical import name used by main.py, but install V5 once.
    if getattr(runtime, "_okai_structural_exit_v5", False):
        return

    dynamic_exit.detect_structural_reversal = _detect_structural_reversal_v5
    backtest_routes.detect_structural_reversal = _detect_structural_reversal_v5
    runtime._evaluate_exit = _runtime_evaluate_exit_v5

    patched, diagnostic = _patch_captured_single_index_backtest()
    runtime._okai_structural_exit_v2 = True
    runtime._okai_structural_exit_v3 = True
    runtime._okai_structural_exit_v4 = True
    runtime._okai_structural_exit_v5 = True
    runtime._okai_structural_exit_backtest_patched = bool(patched)
    runtime._okai_structural_exit_backtest_diagnostic = diagnostic
