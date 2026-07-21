"""Balanced Exit V2 for Backtest, Paper and Live.

Goals:
- normal-day premium stop capped at 6%; expiry remains 8%;
- first true break-even trail only after the trade reaches +0.80R;
- stronger profit ladder: +0.60R, +1.10R, then peak-minus-0.60R;
- no-follow-through exit after three completed candles when peak stayed below
  +0.30R and at least two of VWAP/Supertrend/EMA have flipped opposite;
- ten-minute cooldown after a loss; and
- post-loss quality gate of 85 after one loss and 88 after two or more losses.

The protected normal entry threshold remains 82.  The 85/88 thresholds are a
separate re-entry safety gate and apply only after consecutive losses.
"""

from datetime import datetime, timezone
import inspect
import math

from backtest import routes
from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime
from bot import dynamic_exit
from bot import strategy


TICK_SIZE = 0.05
NORMAL_PREMIUM_RISK_PERCENT = 6.0
EXPIRY_PREMIUM_RISK_PERCENT = 8.0
TRUE_BE_ACTIVATION_R = 0.80
NO_FOLLOW_THROUGH_CANDLES = 3
NO_FOLLOW_THROUGH_MAX_PEAK_R = 0.30
LOSS_COOLDOWN_SECONDS = 10 * 60


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
    seconds = max(0.0, (candle_dt - entry_dt).total_seconds())
    return int(seconds // 60)


def _runtime_evaluate_exit_balanced(trade, ltp, market_data, candle_id):
    """Runtime exit with small-loss no-follow-through protection."""
    from bot.structural_exit_v2_patch import (
        STRUCTURAL_LOSS_FAST_EXIT_R,
        STRUCTURAL_NORMAL_CONFIRM_CANDLES,
        structural_flip,
    )

    entry = _f(trade["entry_price"])
    old_sl = _f(runtime._v(trade, "sl_price", max(TICK_SIZE, entry - TICK_SIZE)))
    risk = max(
        TICK_SIZE,
        _f(runtime._v(trade, "initial_risk", max(TICK_SIZE, entry - old_sl))),
    )
    peak = _f(runtime._v(trade, "peak_price", entry))
    updates = _i(runtime._v(trade, "trail_updates", 0))

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

    previous_count = _i(runtime._v(trade, "reversal_count", 0))
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
        structure["all_three_flipped"]
        and reversal_count >= required
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
            "mode": "VWAP_ST_EMA_ADAPTIVE_BALANCED_V2",
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
    """Compile the captured day backtest with Balanced V2 path logic."""
    target = getattr(routes, "_OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST", None)
    if not callable(target):
        return False, "CAPTURED_SINGLE_INDEX_FUNCTION_MISSING"

    try:
        source = inspect.getsource(target)
        changed = source
        changed = changed.replace(
            "def run_realistic_day_backtest(",
            "def _okai_single_index_balanced_exit_v2(",
            1,
        )
        changed = changed.replace(
            "def _okai_single_index_premium_path_v4(",
            "def _okai_single_index_balanced_exit_v2(",
            1,
        )

        # Keep Structural Exit V4 and point-based option response.
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
            '"mode": "VWAP_ST_EMA_ADAPTIVE_BALANCED_V2"',
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
        new_premium_block = '''            # Balanced V2 synthetic option path uses spot POINT movement.
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

        # Ten one-minute candles of cooldown after each losing exit.
        changed = changed.replace(
            "    consecutive_losses = 0\n",
            "    consecutive_losses = 0\n"
            "    last_loss_exit_index = None\n",
            1,
        )
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
        changed = changed.replace(entry_gate, balanced_entry_gate)

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
            '"premium_response_factor": response,',
            '"premium_response_factor": response,\n'
            '                    "premium_model": "SPOT_POINTS_X_OPTION_RESPONSE_BALANCED_V2",',
        )

        required_markers = (
            "NO_FOLLOW_THROUGH_EXIT_3C_2OF3",
            "loss_cooldown_active",
            "SPOT_POINTS_X_OPTION_RESPONSE_BALANCED_V2",
            "_okai_single_index_balanced_exit_v2",
        )
        if any(marker not in changed for marker in required_markers):
            return False, "BALANCED_SOURCE_TRANSFORM_INCOMPLETE"
        if "response = 8.0" in changed:
            return False, "LEGACY_PERCENT_RESPONSE_STILL_PRESENT"

        exec(
            compile(changed, routes.__file__, "exec"),
            routes.__dict__,
        )
        patched = routes.__dict__.get(
            "_okai_single_index_balanced_exit_v2"
        )
        if not callable(patched):
            return False, "BALANCED_FUNCTION_NOT_CREATED"

        routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST = patched
        try:
            routes._OKAI_BACKTEST_CANDLE_CACHE.clear()
        except Exception:
            pass
        return True, "OK_BALANCED_EXIT_V2"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}:{str(exc)[:180]}"


def apply_balanced_backtest_exit_patch():
    if getattr(runtime, "_okai_balanced_backtest_exit_v2", False):
        return
    runtime._evaluate_exit = _runtime_evaluate_exit_balanced
    patched, diagnostic = _patch_captured_single_index_backtest()
    runtime._okai_balanced_backtest_exit_v2 = True
    runtime._okai_balanced_backtest_exit_patched = bool(patched)
    runtime._okai_balanced_backtest_exit_diagnostic = diagnostic


def calculate_balanced_atr_levels(
    spot_price,
    option_entry_price,
    spot_atr,
    is_expiry_day=False,
    sl_floor_percent=0.0,
    reward_multiple=0.0,
):
    """ATR stop with 6% normal and 8% expiry hard premium cap."""
    entry = max(TICK_SIZE, _f(option_entry_price, TICK_SIZE))
    atr = max(0.0, _f(spot_atr, 0))
    if is_expiry_day:
        response = 1.0
        multiplier = 1.5
        cap_percent = EXPIRY_PREMIUM_RISK_PERCENT
        mode = "EXPIRY_PURE_ATR_BALANCED_8PCT"
    else:
        response = 0.5
        multiplier = 1.2
        cap_percent = NORMAL_PREMIUM_RISK_PERCENT
        mode = "NORMAL_PURE_ATR_BALANCED_6PCT"

    option_atr = atr * response
    raw_risk = option_atr * multiplier
    premium_cap = entry * cap_percent / 100.0
    risk = min(
        max(TICK_SIZE, raw_risk),
        max(TICK_SIZE, premium_cap),
        max(TICK_SIZE, entry - TICK_SIZE),
    )
    return {
        "mode": mode,
        "spot_price": round(_f(spot_price), 2),
        "spot_atr": round(atr, 2),
        "atr_available": atr > 0,
        "response_factor": response,
        "estimated_option_atr": round(option_atr, 2),
        "atr_multiplier": multiplier,
        "atr_risk_points": round(raw_risk, 2),
        "percentage_risk_points": round(premium_cap, 2),
        "sl_floor_percent": 0.0,
        "risk_points": round(risk, 2),
        "sl_price": round(max(TICK_SIZE, entry - risk), 2),
        "target_price": None,
        "reward_multiple": None,
        "is_expiry_day": bool(is_expiry_day),
        "fixed_target_enabled": False,
        "hard_premium_risk_cap_percent": cap_percent,
        "hard_risk_cap_applied": bool(risk + 1e-9 < raw_risk),
        "quantity_preserved": True,
        "balanced_exit_version": "V2",
    }


def _balanced_ladder(
    entry_price,
    initial_risk,
    current_sl,
    peak_price,
    current_price,
    true_be,
    mode_label,
):
    entry = max(TICK_SIZE, _f(entry_price, TICK_SIZE))
    risk = max(TICK_SIZE, _f(initial_risk, TICK_SIZE))
    old_sl = max(TICK_SIZE, _f(current_sl, entry - risk))
    current = max(TICK_SIZE, _f(current_price, TICK_SIZE))
    peak = max(entry, _f(peak_price, entry), current)
    peak_r = (peak - entry) / risk
    be_price = _f(true_be.get("price"), entry)

    new_sl = old_sl
    stage = "INITIAL_ATR_BALANCED_V2"
    locked_r = (old_sl - entry) / risk
    triggered = bool(
        peak_r + 1e-9 >= TRUE_BE_ACTIVATION_R
        and peak + 1e-9 >= be_price + TICK_SIZE
    )

    if triggered:
        new_sl = max(new_sl, be_price)
        stage = "TRUE_BE_PLUS_2PCT_AFTER_0_80R"
        if peak_r >= 1.20:
            new_sl = max(new_sl, entry + 0.60 * risk)
            stage = "LOCK_0_60R_AFTER_1_20R"
        if peak_r >= 1.80:
            new_sl = max(new_sl, entry + 1.10 * risk)
            stage = "LOCK_1_10R_AFTER_1_80R"
        if peak_r >= 2.50:
            new_sl = max(new_sl, peak - 0.60 * risk)
            stage = "TRAIL_PEAK_MINUS_0_60R_AFTER_2_50R"
        locked_r = (new_sl - entry) / risk

    peak_room = max(TICK_SIZE, peak - TICK_SIZE)
    candidate = min(new_sl, peak_room)
    if triggered and candidate + 1e-9 < be_price:
        candidate = old_sl
        stage = "WAITING_TRUE_BE_PRICE_ROOM_BALANCED_V2"
        locked_r = (candidate - entry) / risk
        triggered = False

    return {
        "sl_price": round(candidate, 2),
        "old_sl_price": round(old_sl, 2),
        "updated": candidate > old_sl + 1e-9,
        "peak_price": round(peak, 2),
        "peak_r": round(peak_r, 2),
        "locked_r": round(locked_r, 2),
        "stage": stage,
        "initial_risk": round(risk, 2),
        "cost_safe_breakeven_price": round(be_price, 2),
        "breakeven_triggered": bool(triggered),
        "breakeven_activation_r": TRUE_BE_ACTIVATION_R,
        "breakeven_rule": "ENTRY_PLUS_EXACT_TRADE_COSTS_PLUS_2PCT_NET",
        "breakeven_target_net_profit": true_be.get("target_net_profit"),
        "breakeven_net_pnl_at_stop": true_be.get("net_pnl_at_price"),
        "breakeven_total_charges": true_be.get("total_charges_at_price"),
        "breakeven_slippage_cost": true_be.get("slippage_cost_at_price", 0),
        "breakeven_quantity_basis": true_be.get("quantity_basis"),
        "breakeven_instrument_basis": true_be.get("instrument_basis"),
        "breakeven_broker_basis": true_be.get("broker_basis"),
        "breakeven_trading_mode_basis": true_be.get("trading_mode_basis"),
        "balanced_exit_version": "V2",
        "balanced_context": mode_label,
    }


def _backtest_balanced_profit_lock(
    entry_price,
    initial_risk,
    current_sl,
    peak_price,
    current_price,
):
    from backtest import cost_safe_breakeven_risk_patch as backtest_be

    ctx = backtest_be._context_value()
    true_be = backtest_be.calculate_cost_safe_breakeven_price(
        ctx["broker_name"],
        ctx["instrument"],
        max(TICK_SIZE, _f(entry_price, TICK_SIZE)),
        ctx["quantity"],
        backtest_be.NET_PROFIT_LOCK_PERCENT,
    )
    return _balanced_ladder(
        entry_price,
        initial_risk,
        current_sl,
        peak_price,
        current_price,
        true_be,
        "BACKTEST_ESTIMATED_COSTS",
    )


def _live_balanced_profit_lock(
    entry_price,
    initial_risk,
    current_sl,
    peak_price,
    current_price,
):
    from bot import live_net_pnl_breakeven_patch as live_be

    ctx = live_be._current_context()
    true_be = live_be.calculate_exact_breakeven_price(
        ctx["broker_name"],
        ctx["instrument"],
        round(max(TICK_SIZE, _f(entry_price, TICK_SIZE)), 4),
        ctx["quantity"],
        ctx["mode"],
        live_be.NET_PROFIT_LOCK_PERCENT,
    )
    return _balanced_ladder(
        entry_price,
        initial_risk,
        current_sl,
        peak_price,
        current_price,
        true_be,
        "PAPER_LIVE_EXACT_TRADE_COSTS",
    )


def _post_loss_required_score(losses):
    count = max(0, _i(losses, 0))
    if count >= 2:
        return 88
    if count == 1:
        return 85
    return 82


def _install_post_loss_score_gate():
    if getattr(strategy, "_okai_balanced_post_loss_gate_v2", False):
        return

    original = strategy.get_full_signal

    def balanced_signal(market_data, consecutive_losses=0, profile=None):
        result = dict(
            original(
                market_data,
                consecutive_losses=consecutive_losses,
                profile=profile,
            )
        )
        required = max(
            _i(result.get("min_score"), 82),
            _post_loss_required_score(consecutive_losses),
        )
        candidate = str(result.get("candidate_signal") or result.get("signal") or "WAIT")
        score = _i(result.get("score"), 0)
        warnings = list(result.get("warnings") or [])
        if candidate in ("CE", "PE") and score < required:
            result["signal"] = "WAIT"
            result["trade_allowed"] = False
            warnings.append(
                f"POST_LOSS_SCORE_GATE:{score}<{required}"
            )
        result["warnings"] = warnings
        result["min_score"] = required
        result["post_loss_required_score"] = required
        result["post_loss_gate_active"] = _i(consecutive_losses, 0) > 0
        return result

    strategy.get_full_signal = balanced_signal
    angel_fetcher.get_full_signal = balanced_signal
    routes.get_full_signal = balanced_signal
    strategy._okai_balanced_post_loss_gate_v2 = True


def _install_runtime_loss_cooldown():
    if getattr(runtime, "_okai_balanced_loss_cooldown_v2", False):
        return

    original_ensure = runtime._ensure_schema
    original_close = runtime._close
    original_can_enter = runtime._can_enter

    def ensure_with_closed_at(conn):
        original_ensure(conn)
        try:
            conn.execute(
                "ALTER TABLE paper_trades ADD COLUMN closed_at TEXT"
            )
        except Exception:
            pass
        conn.commit()

    def close_with_timestamp(
        conn,
        user_id,
        trade,
        price,
        reason,
        order_id=None,
    ):
        result = original_close(
            conn,
            user_id,
            trade,
            price,
            reason,
            order_id,
        )
        try:
            conn.execute(
                "UPDATE paper_trades SET closed_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), trade["id"]),
            )
            conn.commit()
        except Exception:
            pass
        return result

    def can_enter_after_cooldown(conn, user_id, settings, rows, state):
        if not original_can_enter(conn, user_id, settings, rows, state):
            return False
        try:
            row = conn.execute(
                """
                SELECT closed_at
                FROM paper_trades
                WHERE user_id=?
                  AND status='CLOSED'
                  AND pnl < 0
                  AND closed_at IS NOT NULL
                ORDER BY datetime(closed_at) DESC, id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            closed_at = _parse_dt(row["closed_at"] if row else None)
            if closed_at is None:
                state.pop("loss_cooldown", None)
                return True
            elapsed = (
                datetime.now(timezone.utc) - closed_at.astimezone(timezone.utc)
            ).total_seconds()
            remaining = max(0, int(math.ceil(LOSS_COOLDOWN_SECONDS - elapsed)))
            if remaining > 0:
                state["loss_cooldown"] = {
                    "active": True,
                    "remaining_seconds": remaining,
                    "rule": "10_MINUTES_AFTER_LOSS",
                }
                return False
            state.pop("loss_cooldown", None)
            return True
        except Exception:
            return True

    runtime._ensure_schema = ensure_with_closed_at
    runtime._close = close_with_timestamp
    runtime._can_enter = can_enter_after_cooldown
    runtime._okai_balanced_loss_cooldown_v2 = True


def apply_balanced_risk_profit_patch():
    """Install final functions after exact-cost and quantity patches."""
    if getattr(runtime, "_okai_balanced_risk_profit_v2", False):
        return

    dynamic_exit.calculate_option_atr_levels = calculate_balanced_atr_levels
    strategy.calculate_option_atr_levels = calculate_balanced_atr_levels
    angel_fetcher.calculate_option_atr_levels = calculate_balanced_atr_levels
    angel_fetcher._dynamic_atr_levels = calculate_balanced_atr_levels
    routes.calculate_option_atr_levels = calculate_balanced_atr_levels

    routes.update_option_profit_lock = _backtest_balanced_profit_lock
    dynamic_exit.update_option_profit_lock = _live_balanced_profit_lock
    strategy.update_option_profit_lock = _live_balanced_profit_lock
    angel_fetcher.update_option_profit_lock = _live_balanced_profit_lock
    angel_fetcher._dynamic_profit_lock = _live_balanced_profit_lock

    try:
        from backtest import cost_safe_breakeven_risk_patch as backtest_be
        backtest_be._cost_safe_profit_lock = _backtest_balanced_profit_lock
    except Exception:
        pass

    _install_post_loss_score_gate()
    _install_runtime_loss_cooldown()

    runtime._okai_balanced_risk_profit_v2 = True
    runtime._okai_normal_premium_risk_cap_percent = NORMAL_PREMIUM_RISK_PERCENT
    runtime._okai_expiry_premium_risk_cap_percent = EXPIRY_PREMIUM_RISK_PERCENT
    runtime._okai_true_be_activation_r = TRUE_BE_ACTIVATION_R
