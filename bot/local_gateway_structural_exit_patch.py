"""Live structural-exit parity for the owner's static-IP gateway.

The local phone owns broker orders and option-premium SL/trailing. The Railway
strategy loop owns completed index candles, so it evaluates VWAP + Supertrend +
EMA structure and queues an EXIT_POSITION command to the local phone.
"""

import json
from datetime import datetime, timezone

from bot import angel_fetcher
from database import get_db
from local_gateway.service import queue_exit


FAST_EXIT_R = -0.35
NORMAL_CONFIRM_CANDLES = 2
NO_FOLLOW_THROUGH_CANDLES = 3
NO_FOLLOW_THROUGH_MAX_PEAK_R = 0.30


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _parse_dt(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _load_metadata(value):
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _structure(side, market_data):
    market = market_data or {}
    side = str(side or "").upper()
    price = _f(market.get("price"), 0)
    vwap = _f(market.get("vwap"), price)
    ema9 = _f(market.get("ema9"), price)
    ema21 = _f(market.get("ema21"), ema9)
    supertrend = str(market.get("supertrend_dir") or "NEUTRAL").upper()

    if side == "CE":
        checks = {
            "vwap_opposite": price < vwap,
            "supertrend_opposite": supertrend == "DOWN",
            "ema_opposite": ema9 < ema21,
        }
    elif side == "PE":
        checks = {
            "vwap_opposite": price > vwap,
            "supertrend_opposite": supertrend == "UP",
            "ema_opposite": ema9 > ema21,
        }
    else:
        checks = {
            "vwap_opposite": False,
            "supertrend_opposite": False,
            "ema_opposite": False,
        }

    opposite_count = sum(1 for passed in checks.values() if passed)
    return {
        **checks,
        "opposite_count": opposite_count,
        "all_three_flipped": opposite_count == 3,
        "price": round(price, 2),
        "vwap": round(vwap, 2),
        "ema9": round(ema9, 2),
        "ema21": round(ema21, 2),
        "supertrend_dir": supertrend,
    }


def _manage_existing_positions(user_id, underlying, market_data, candle_id):
    if not market_data or not candle_id:
        return []

    conn = get_db()
    queued = []
    try:
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE user_id=? AND underlying=? AND status='open'
            ORDER BY id ASC
            """,
            (int(user_id), str(underlying or "").upper()),
        ).fetchall()

        for trade in rows:
            metadata = _load_metadata(trade["metadata_json"])
            gateway = metadata.get("gateway_position") or {}
            structural = metadata.get("structural_exit") or {}
            side = str(trade["side"] or trade["option_type"] or "").upper()
            state = _structure(side, market_data)

            current_candle = str(candle_id or "")
            previous_candle = str(structural.get("last_candle") or "")
            previous_count = int(structural.get("count") or 0)
            if current_candle != previous_candle:
                count = previous_count + 1 if state["all_three_flipped"] else 0
            else:
                count = previous_count

            entry = _f(trade["entry_price"], 0)
            active_sl = _f(gateway.get("active_sl"), _f(trade["sl_price"], 0))
            initial_sl = _f(gateway.get("initial_sl"), _f(trade["sl_price"], 0))
            risk = max(0.05, entry - initial_sl) if entry > 0 else 0.05
            ltp = _f(gateway.get("ltp"), entry)
            current_r = (ltp - entry) / risk if entry > 0 else 0.0
            peak_r = _f(gateway.get("peak_r"), 0)

            entry_time = _parse_dt(trade["entry_time"])
            candle_time = _parse_dt(current_candle)
            bars_held = 0
            if entry_time and candle_time:
                bars_held = max(
                    0,
                    int((candle_time - entry_time).total_seconds() // 60),
                )

            required = 1 if current_r <= FAST_EXIT_R else NORMAL_CONFIRM_CANDLES
            structural_exit = bool(
                state["all_three_flipped"] and count >= required
            )
            no_follow_through = bool(
                bars_held >= NO_FOLLOW_THROUGH_CANDLES
                and peak_r < NO_FOLLOW_THROUGH_MAX_PEAK_R
                and state["opposite_count"] >= 2
            )

            reason = None
            if no_follow_through:
                reason = (
                    "NO FOLLOW THROUGH EXIT"
                    f" | bars={bars_held}"
                    f" | peak={peak_r:.2f}R"
                    f" | weakness={state['opposite_count']}/3"
                )
            elif structural_exit:
                reason = (
                    "VWAP + SUPERTREND + EMA STRUCTURAL EXIT"
                    f" | confirmations={count}/{required}"
                    f" | current_r={current_r:.2f}"
                )

            metadata["structural_exit"] = {
                **state,
                "count": count,
                "required": required,
                "last_candle": current_candle,
                "current_r": round(current_r, 2),
                "peak_r": round(peak_r, 2),
                "bars_held": bars_held,
                "active_sl": active_sl,
                "exit_triggered": bool(reason),
                "reason": reason,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "mode": "LOCAL_GATEWAY_VWAP_ST_EMA_V1",
            }
            conn.execute(
                "UPDATE trades SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, separators=(",", ":")), trade["id"]),
            )
            if reason:
                queued.append((int(trade["id"]), reason))
        conn.commit()
    finally:
        conn.close()

    results = []
    for trade_id, reason in queued:
        results.append(queue_exit(user_id, trade_id, reason))
    return results


def apply_local_gateway_structural_exit_patch():
    if getattr(angel_fetcher, "_okai_local_gateway_structural_exit_v1", False):
        return

    original = angel_fetcher._manage_live_gateway_entry

    def managed_live_gateway_entry(
        user_id,
        underlying,
        price,
        side,
        score,
        trade_allowed,
        settings,
        obj,
        spot_atr=0.0,
        market_data=None,
        candle_id=None,
    ):
        exit_results = _manage_existing_positions(
            user_id,
            underlying,
            market_data,
            candle_id,
        )
        result = original(
            user_id=user_id,
            underlying=underlying,
            price=price,
            side=side,
            score=score,
            trade_allowed=trade_allowed,
            settings=settings,
            obj=obj,
            spot_atr=spot_atr,
            market_data=market_data,
            candle_id=candle_id,
        )
        if isinstance(result, dict) and exit_results:
            result = dict(result)
            result["structural_exit_commands"] = exit_results
        return result

    angel_fetcher._manage_live_gateway_entry = managed_live_gateway_entry
    angel_fetcher._okai_local_gateway_structural_exit_v1 = True
