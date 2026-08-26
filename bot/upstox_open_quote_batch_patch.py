"""Batch Upstox OPEN-position LTP reads per monitor cycle.

Avoid one market-quote request per open trade. Successful batched quotes also
stamp quote_updated_at directly here so freshness cannot depend on patch order.
Entry, exit, SL, sizing and strategy logic remain unchanged.
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone

_PATCHED = False
_LOCK = threading.Lock()


def _closure_values(fn):
    values = []
    try:
        for cell in (getattr(fn, "__closure__", None) or ()):
            try:
                values.append(cell.cell_contents)
            except Exception:
                pass
    except Exception:
        pass
    return values


def _upstox_obj_from_fetcher(fetcher):
    broker_name = None
    obj = None
    for value in _closure_values(fetcher):
        if isinstance(value, str) and value.lower() == "upstox":
            broker_name = "upstox"
        elif callable(getattr(value, "get_ltps", None)) and callable(getattr(value, "get_ltp", None)):
            obj = value
    if broker_name == "upstox" and obj is not None:
        return obj
    return None


def _batch_quotes(runtime, obj, rows):
    by_exchange = {}
    trade_ref = {}
    for trade in rows or []:
        ref = runtime._v(trade, "token", trade["symbol"]) or trade["symbol"]
        exchange = runtime._v(trade, "exch_seg", "NSE_FO") or "NSE_FO"
        ref = str(ref)
        exchange = str(exchange)
        trade_ref[trade["id"]] = (ref, exchange)
        by_exchange.setdefault(exchange, []).append(ref)

    output = {}
    for exchange, refs in by_exchange.items():
        unique = list(dict.fromkeys(refs))
        try:
            result = obj.get_ltps(unique, exchange=exchange) or {}
        except Exception as exc:
            result = {"success": False, "message": str(exc)}

        quotes = result.get("quotes") or {}
        if not result.get("success"):
            failure = {
                "success": False,
                "message": result.get("message") or "UPSTOX_BATCH_LTP_FAILED",
            }
            if result.get("rate_limited"):
                failure["rate_limited"] = True
                failure["retry_after_seconds"] = result.get("retry_after_seconds", 45)
            for trade in rows or []:
                ref, ex = trade_ref.get(trade["id"], (None, None))
                if ex == exchange:
                    output[trade["id"]] = dict(failure)
            continue

        for trade in rows or []:
            ref, ex = trade_ref.get(trade["id"], (None, None))
            if ex != exchange:
                continue
            quote = quotes.get(ref)
            if quote and quote.get("success"):
                output[trade["id"]] = dict(quote)
            else:
                output[trade["id"]] = {
                    "success": False,
                    "message": f"No batched LTP data for {ref}",
                }
    return output


def _stamp_success(conn, trade, result):
    try:
        ltp = float((result or {}).get("ltp") or 0)
        if not (isinstance(result, dict) and result.get("success") and ltp > 0):
            return
        conn.execute(
            """
            UPDATE paper_trades
            SET quote_updated_at=?, quote_source=?,
                quote_failed_at=NULL, quote_error=NULL,
                quote_failure_count=0
            WHERE id=? AND status='OPEN'
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                "UPSTOX_BATCH_LTP_V2",
                trade["id"],
            ),
        )
        conn.commit()
    except Exception:
        pass


def _patch(runtime):
    global _PATCHED
    with _LOCK:
        if _PATCHED:
            return
        original = getattr(runtime, "_manage_rows", None)
        if not callable(original):
            return

        def manage_rows_batched(conn, user_id, rows, scans, quote_fetcher, live_order, state):
            obj = _upstox_obj_from_fetcher(quote_fetcher)
            if obj is None or not rows:
                return original(conn, user_id, rows, scans, quote_fetcher, live_order, state)

            batched = _batch_quotes(runtime, obj, rows)

            def fetch_from_batch(trade):
                result = batched.get(trade["id"])
                if result is not None:
                    _stamp_success(conn, trade, result)
                    return result
                result = quote_fetcher(trade)
                _stamp_success(conn, trade, result)
                return result

            state["open_quote_transport"] = "UPSTOX_BATCH_LTP_V2"
            return original(conn, user_id, rows, scans, fetch_from_batch, live_order, state)

        runtime._manage_rows = manage_rows_batched
        runtime.UPSTOX_OPEN_QUOTE_BATCH_PATCH = "V2"
        _PATCHED = True


def _wait_and_patch():
    for _ in range(240):
        runtime = sys.modules.get("bot.auto_portfolio_runtime")
        if runtime is not None:
            try:
                _patch(runtime)
                if _PATCHED:
                    return
            except Exception:
                pass
        time.sleep(0.25)


def schedule_upstox_open_quote_batch_patch():
    threading.Thread(
        target=_wait_and_patch,
        name="okai-upstox-open-quote-batch",
        daemon=True,
    ).start()
