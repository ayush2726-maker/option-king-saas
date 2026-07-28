"""Balanced runtime exit patch V4.

Entry strategy remains unchanged. This only changes exit handling, 4% cost-safe
breakeven and post-risk-exit cooldowns.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from backtest import post_loss_reentry_cooldown_patch as backtest_reentry
from bot import angel_fetcher as legacy
from bot import auto_portfolio_runtime as runtime
from bot import dynamic_exit
from bot import post_loss_reentry_guard_patch as runtime_reentry


BREAKEVEN_NET_PROFIT_PERCENT = 4.0
BREAKEVEN_TRIGGER_R = 0.80
FIRST_LOSS_COOLDOWN_MINUTES = 12
SECOND_LOSS_COOLDOWN_MINUTES = 20
FIRST_LOSS_REASON = "POST_RISK_EXIT_SAME_SIDE_COOLDOWN_12M"
SECOND_LOSS_REASON = "POST_RISK_EXIT_SAME_INDEX_COOLDOWN_20M_AFTER_2"
THIRD_LOSS_REASON = "POST_RISK_EXIT_INDEX_BLOCK_REST_OF_DAY_AFTER_3"
EARLY_EXIT_REASON = "EARLY DANGER EXIT BEFORE SL"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _now_utc():
    return datetime.now(timezone.utc)


def _ist_day_bounds_utc(now=None):
    current = now or _now_utc()
    ist = timezone(timedelta(hours=5, minutes=30))
    current_ist = current.astimezone(ist)
    start_ist = current_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    end_ist = start_ist + timedelta(days=1)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def _balanced_profit_lock(entry_price, initial_risk, current_sl, peak_price, current_price):
    entry = max(dynamic_exit.TICK_SIZE, _f(entry_price, dynamic_exit.TICK_SIZE))
    risk = max(dynamic_exit.TICK_SIZE, _f(initial_risk, dynamic_exit.TICK_SIZE))
    old_sl = max(dynamic_exit.TICK_SIZE, _f(current_sl, entry - risk))
    current = max(dynamic_exit.TICK_SIZE, _f(current_price, dynamic_exit.TICK_SIZE))
    peak = max(entry, _f(peak_price, entry), current)
    peak_r = round((peak - entry) / risk, 10)

    true_be = dynamic_exit.calculate_cost_safe_breakeven_price(
        entry,
        BREAKEVEN_NET_PROFIT_PERCENT,
    )
    be_price = float(true_be["price"])
    new_sl = old_sl
    stage = "INITIAL_ATR"
    locked_r = -1.0
    breakeven_triggered = False

    if peak_r >= BREAKEVEN_TRIGGER_R and peak + 1e-9 >= be_price + dynamic_exit.TICK_SIZE:
        new_sl = max(new_sl, be_price)
        stage = "COST_PLUS_4PCT_BREAKEVEN_AFTER_0_8R"
        locked_r = (new_sl - entry) / risk
        breakeven_triggered = True

        if peak_r >= 1.50:
            new_sl = max(new_sl, entry + 0.75 * risk)
            stage = "LOCK_0_75R_AFTER_1_5R"
            locked_r = (new_sl - entry) / risk

        if peak_r >= 2.00:
            new_sl = max(new_sl, entry + 1.25 * risk)
            stage = "LOCK_1_25R_AFTER_2R"
            locked_r = (new_sl - entry) / risk

        if peak_r >= 2.50:
            new_sl = max(new_sl, entry + 1.25 * risk, peak - 0.75 * risk)
            stage = "DYNAMIC_TRAIL_AFTER_2_5R"
            locked_r = (new_sl - entry) / risk

    peak_room = max(dynamic_exit.TICK_SIZE, peak - dynamic_exit.TICK_SIZE)
    candidate = min(new_sl, peak_room)
    if breakeven_triggered and candidate + 1e-9 < be_price:
        candidate = old_sl
        stage = "WAITING_4PCT_BE_PRICE_ROOM"
        locked_r = (candidate - entry) / risk
        breakeven_triggered = False

    candidate = max(old_sl, candidate)
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
        "breakeven_triggered": bool(breakeven_triggered),
        "breakeven_rule": "ENTRY_PLUS_ALL_COSTS_PLUS_4PCT_NET_AFTER_0_8R",
        "breakeven_target_net_profit": true_be["target_net_profit"],
        "breakeven_net_pnl_at_stop": true_be["net_pnl_at_price"],
        "breakeven_total_charges": true_be["total_charges_at_price"],
    }


def _early_danger_exit(trade, ltp, market_data, trail):
    if not market_data:
        return {"exit": False, "reason": None}

    side = str(runtime._v(trade, "side", "") or "").upper()
    if side not in ("CE", "PE"):
        return {"exit": False, "reason": None}

    entry = max(0.05, _f(runtime._v(trade, "entry_price", 0), 0.05))
    risk = max(0.05, _f(runtime._v(trade, "initial_risk", 0.05), 0.05))
    current = max(0.05, _f(ltp, entry))
    peak = max(entry, _f(trail.get("peak_price"), entry), current)
    active_sl = max(0.05, _f(trail.get("sl_price"), entry - risk))

    loss_r = (entry - current) / risk
    peak_r = (peak - entry) / risk
    giveback_r = (peak - current) / risk
    near_sl = current <= active_sl + 0.25 * risk
    premium_failing = (
        loss_r >= 0.45
        or near_sl
        or (peak_r >= 0.80 and giveback_r >= 0.70 and current <= entry + 0.10 * risk)
    )
    if not premium_failing:
        return {"exit": False, "reason": None}

    close = _f(market_data.get("price"), 0)
    vwap = _f(market_data.get("vwap"), close)
    ema9 = _f(market_data.get("ema9"), close)
    ema21 = _f(market_data.get("ema21"), ema9)
    st = str(market_data.get("supertrend_dir") or "NEUTRAL").upper()

    if side == "CE":
        vwap_break = close < vwap
        ema_break = close < ema9
        trend_against = st == "DOWN" or ema9 < ema21
    else:
        vwap_break = close > vwap
        ema_break = close > ema9
        trend_against = st == "UP" or ema9 > ema21

    if not (vwap_break and ema_break and trend_against):
        return {"exit": False, "reason": None}

    return {
        "exit": True,
        "reason": (
            f"{EARLY_EXIT_REASON} | premium_loss={round(loss_r, 2)}R"
            f" | giveback={round(giveback_r, 2)}R | ST={st}"
        ),
    }


def _runtime_evaluate_exit(trade, ltp, market_data, candle_id):
    entry = runtime._f(trade["entry_price"])
    old_sl = runtime._f(runtime._v(trade, "sl_price", max(0.05, entry - 0.05)))
    risk = runtime._f(runtime._v(trade, "initial_risk", max(0.05, entry - old_sl)))
    peak = runtime._f(runtime._v(trade, "peak_price", entry))
    updates = runtime._i(runtime._v(trade, "trail_updates", 0))

    trail = legacy._dynamic_profit_lock(entry, risk, old_sl, peak, ltp)
    if trail["updated"]:
        updates += 1

    reversal = legacy._dynamic_reversal_state(trade, market_data, candle_id)
    danger = _early_danger_exit(trade, ltp, market_data, trail)
    eod = runtime._now_ist().hour * 60 + runtime._now_ist().minute >= 15 * 60 + 25
    hit = ltp <= trail["sl_price"]

    if hit and trail["sl_price"] >= entry:
        reason = "PROFIT LOCK TRAIL HIT" f" | {trail['stage']} | locked={trail['locked_r']}R"
    elif hit:
        reason = "PURE ATR SL HIT"
    elif danger.get("exit"):
        reason = danger["reason"]
    elif reversal["exit"]:
        reason = f"TWO CANDLE STRUCTURAL REVERSAL EXIT | count={reversal['count']}"
    elif eod:
        reason = "EOD EXIT 15:25 IST"
    else:
        reason = None

    return {
        "trail": trail,
        "risk": risk,
        "updates": updates,
        "reversal": reversal,
        "early_danger": danger,
        "reason": reason,
    }


def _is_risk_exit(reason):
    text = str(reason or "").upper()
    return "PURE ATR SL" in text or EARLY_EXIT_REASON in text


def _upsert_block(conn, user_id, underlying, side, blocked_until, source_trade_id, reason, now):
    conn.execute(
        """
        INSERT INTO auto_reentry_blocks (
            user_id, underlying, side, blocked_until,
            source_trade_id, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, underlying, side)
        DO UPDATE SET
            blocked_until=excluded.blocked_until,
            source_trade_id=excluded.source_trade_id,
            reason=excluded.reason,
            created_at=excluded.created_at
        """,
        (
            int(user_id), underlying, side,
            runtime_reentry._iso(blocked_until), source_trade_id, reason,
            runtime_reentry._iso(now),
        ),
    )


def _register_balanced_loss_block(conn, user_id, trade, reason):
    if not _is_risk_exit(reason):
        return

    underlying = runtime._underlying(trade)
    side = str(runtime._v(trade, "side", "") or "").upper()
    if side not in ("CE", "PE"):
        return

    runtime_reentry._ensure_guard_schema(conn)
    now = _now_utc()
    start_utc, end_utc = _ist_day_bounds_utc(now)
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM paper_trades
        WHERE user_id=?
          AND status='CLOSED'
          AND datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
          AND UPPER(COALESCE(underlying, ''))=?
          AND (
              UPPER(COALESCE(reason, '')) LIKE '%PURE ATR SL%'
              OR UPPER(COALESCE(reason, '')) LIKE '%EARLY DANGER EXIT BEFORE SL%'
          )
        """,
        (int(user_id), start_utc.strftime("%Y-%m-%d %H:%M:%S"), end_utc.strftime("%Y-%m-%d %H:%M:%S"), underlying),
    ).fetchone()
    risk_exit_count = max(1, _i(row["c"] if row else 1, 1))
    source_trade_id = runtime._i(runtime._v(trade, "id", 0), 0)

    if risk_exit_count >= 3:
        blocked_until = end_utc
        block_reason = THIRD_LOSS_REASON
        sides_to_block = ("CE", "PE")
    elif risk_exit_count >= 2:
        blocked_until = now + timedelta(minutes=SECOND_LOSS_COOLDOWN_MINUTES)
        block_reason = SECOND_LOSS_REASON
        sides_to_block = ("CE", "PE")
    else:
        blocked_until = now + timedelta(minutes=FIRST_LOSS_COOLDOWN_MINUTES)
        block_reason = FIRST_LOSS_REASON
        sides_to_block = (side,)

    for blocked_side in sides_to_block:
        _upsert_block(conn, user_id, underlying, blocked_side, blocked_until, source_trade_id, block_reason, now)
    conn.commit()


def _balanced_block_scan(scan, block):
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan
    signal = dict(scan.get("signal_data") or {})
    candidate = str(signal.get("candidate_signal") or signal.get("signal") or "WAIT").upper()
    if candidate not in ("CE", "PE"):
        return scan

    reason = str((block or {}).get("reason") or FIRST_LOSS_REASON)
    reasons = list(signal.get("safety_gate_reasons") or [])
    fresh = list(signal.get("fresh_entry_block_reasons") or [])
    warnings = list(signal.get("warnings") or [])
    for collection in (reasons, fresh, warnings):
        if reason not in collection:
            collection.append(reason)

    signal.update({
        "signal": "WAIT",
        "trade_allowed": False,
        "safety_gate_passed": False,
        "fresh_entry_ok": False,
        "safety_gate_reasons": reasons,
        "fresh_entry_block_reasons": fresh,
        "warnings": warnings,
        "post_loss_reentry_blocked": True,
        "post_loss_reentry_reason": reason,
        "post_loss_reentry_blocked_until": (block or {}).get("blocked_until"),
        "post_loss_source_trade_id": (block or {}).get("source_trade_id"),
    })
    scan["signal_data"] = signal
    market = dict(scan.get("market_data") or {})
    market["signal"] = "WAIT"
    scan["market_data"] = market
    scan["entry_block_reason"] = reason
    return scan


def apply_balanced_exit_cooldown_v4_patch():
    if getattr(runtime, "_okai_balanced_exit_cooldown_v4", False):
        return

    dynamic_exit.TRUE_BE_NET_PROFIT_PERCENT = BREAKEVEN_NET_PROFIT_PERCENT
    dynamic_exit.update_option_profit_lock = _balanced_profit_lock
    legacy._dynamic_profit_lock = _balanced_profit_lock
    runtime._evaluate_exit = _runtime_evaluate_exit

    runtime_reentry.COOLDOWN_SECONDS = FIRST_LOSS_COOLDOWN_MINUTES * 60
    runtime_reentry.BLOCK_REASON = FIRST_LOSS_REASON
    runtime_reentry._register_loss_block = _register_balanced_loss_block
    runtime_reentry._block_scan = _balanced_block_scan

    backtest_reentry.COOLDOWN_MINUTES = FIRST_LOSS_COOLDOWN_MINUTES
    backtest_reentry.COOLDOWN_REASON = FIRST_LOSS_REASON

    runtime._okai_balanced_exit_cooldown_v4 = True
