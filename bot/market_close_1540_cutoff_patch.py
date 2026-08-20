"""Update intraday timing for the extended F&O close.

Fresh AUTO entries stop at 15:25 IST. Existing positions are allowed to run
until 15:35 IST unless SL/target/reversal exits earlier. This keeps a five-minute
buffer before the 15:40 derivatives close.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

VERSION = "OKAI-MARKET-CLOSE-1540-CUTOFF-V2"
ENTRY_CUTOFF_MINUTE = 15 * 60 + 25
FORCE_EXIT_MINUTE = 15 * 60 + 35


def _now_ist():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _minute_now():
    now = _now_ist()
    return now.hour * 60 + now.minute


def apply_market_close_1540_cutoff_patch() -> None:
    from bot import auto_portfolio_runtime as runtime
    from bot import angel_fetcher
    from bot import strategy

    if getattr(runtime, "_okai_market_close_1540_cutoff_v1", False):
        return

    # HERO/status helpers use this module-level constant dynamically.
    strategy.HERO_FORCE_EXIT = (15, 35)

    # --- AUTO Portfolio fresh-entry cutoff ---
    original_open_common = runtime._open_common

    def open_common_with_1525_cutoff(
        conn,
        user_id,
        broker_name,
        selected,
        settings,
        resolved,
        quote_price,
        quality,
        lot_size,
        live_order,
        live_cash,
        state,
    ):
        if _minute_now() >= ENTRY_CUTOFF_MINUTE:
            try:
                return runtime._record_preopen_failure(
                    state,
                    broker_name,
                    selected,
                    "FRESH_ENTRY_CUTOFF_15_25_IST",
                    "TIME_GUARD",
                    {
                        "fresh_entry_cutoff": "15:25",
                        "force_exit": "15:35",
                        "market_close": "15:40",
                        "version": VERSION,
                    },
                )
            except Exception:
                return False
        return original_open_common(
            conn,
            user_id,
            broker_name,
            selected,
            settings,
            resolved,
            quote_price,
            quality,
            lot_size,
            live_order,
            live_cash,
            state,
        )

    runtime._open_common = open_common_with_1525_cutoff

    # --- AUTO Portfolio force exit: suppress old 15:25 EOD exit and use 15:35 ---
    original_evaluate_exit = runtime._evaluate_exit

    def evaluate_exit_with_1535_force_exit(trade, ltp, market_data, candle_id):
        result = dict(original_evaluate_exit(trade, ltp, market_data, candle_id) or {})
        now_minute = _minute_now()
        reason = str(result.get("reason") or "")

        # The old runtime marks EOD at 15:25. Ignore only that time-based reason;
        # SL, profit-lock and structural reversal exits remain untouched.
        if reason.startswith("EOD EXIT") and now_minute < FORCE_EXIT_MINUTE:
            result["reason"] = None
        elif now_minute >= FORCE_EXIT_MINUTE and not result.get("reason"):
            result["reason"] = "EOD EXIT 15:35 IST"
        elif now_minute >= FORCE_EXIT_MINUTE and reason.startswith("EOD EXIT"):
            result["reason"] = "EOD EXIT 15:35 IST"

        return result

    runtime._evaluate_exit = evaluate_exit_with_1535_force_exit

    # --- Legacy Angel live entry: stop fresh entries at 15:25 ---
    original_live_gateway_entry = angel_fetcher._manage_live_gateway_entry

    def live_gateway_entry_with_1525_cutoff(*args, **kwargs):
        if _minute_now() >= ENTRY_CUTOFF_MINUTE:
            return {
                "queued": False,
                "reason": "FRESH_ENTRY_CUTOFF_15_25_IST",
                "fresh_entry_cutoff": "15:25",
                "force_exit": "15:35",
                "version": VERSION,
            }
        return original_live_gateway_entry(*args, **kwargs)

    angel_fetcher._manage_live_gateway_entry = live_gateway_entry_with_1525_cutoff

    # Any local-gateway payload created by legacy live flow must carry 15:35.
    original_queue_live_entry = angel_fetcher.queue_live_entry

    def queue_live_entry_with_1535(user_id, payload, *args, **kwargs):
        safe_payload = dict(payload or {})
        safe_payload["force_exit_at"] = "15:35"
        safe_payload["fresh_entry_cutoff"] = "15:25"
        safe_payload["market_close"] = "15:40"
        return original_queue_live_entry(user_id, safe_payload, *args, **kwargs)

    angel_fetcher.queue_live_entry = queue_live_entry_with_1535

    # Legacy paper manager has a hard-coded 15:25 exit. Between 15:25 and 15:35
    # manage SL/target normally without calling that old EOD branch. At 15:35+
    # the original function is allowed through and closes the trade.
    original_manage_paper_trade = angel_fetcher._manage_paper_trade

    def manage_paper_trade_with_1535(*args, **kwargs):
        minute = _minute_now()
        if minute < 15 * 60 + 25 or minute >= FORCE_EXIT_MINUTE:
            return original_manage_paper_trade(*args, **kwargs)

        user_id = kwargs.get("user_id", args[0] if len(args) > 0 else None)
        settings = kwargs.get("settings", args[6] if len(args) > 6 else {}) or {}
        obj = kwargs.get("obj", args[7] if len(args) > 7 else None)
        if str(settings.get("trading_mode", "paper")).lower() != "paper":
            return original_manage_paper_trade(*args, **kwargs)

        from database import get_db
        conn = get_db()
        try:
            open_trade = conn.execute(
                "SELECT * FROM paper_trades WHERE user_id=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if not open_trade:
                # No position: 15:25 fresh-entry cutoff means do nothing.
                return None

            token = open_trade["token"]
            symbol = open_trade["symbol"]
            exch_seg = open_trade["exch_seg"]
            if not token or not symbol or not exch_seg or obj is None:
                return None

            try:
                quote = obj.ltpData(exch_seg, symbol, token)
                current_ltp = float(quote["data"]["ltp"])
            except Exception:
                return None

            sl = open_trade["sl_price"]
            target = open_trade["target_price"]
            hit_sl = bool(sl and current_ltp <= float(sl))
            hit_target = bool(target and current_ltp >= float(target))
            if not (hit_sl or hit_target):
                return None

            qty = open_trade["qty"] or 1
            entry_price = open_trade["entry_price"] or 0
            pnl = round((current_ltp - entry_price) * qty, 2)
            reason = "TARGET HIT (real premium)" if hit_target else "SL HIT (real premium)"
            conn.execute(
                "UPDATE paper_trades SET exit_price=?, pnl=?, status='CLOSED', reason=? WHERE id=?",
                (current_ltp, pnl, reason, open_trade["id"]),
            )
            conn.commit()
            return None
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    angel_fetcher._manage_paper_trade = manage_paper_trade_with_1535

    runtime._okai_market_close_1540_cutoff_v1 = True
