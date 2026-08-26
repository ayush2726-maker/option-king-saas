"""Batch Upstox OPEN-position LTP reads per monitor cycle.

Avoid one market-quote request per open trade. The old 5-second monitor could
multiply requests across positions/recovery loops and push the Upstox market-data
API into 429/rate-limit behaviour, after which last_ltp stayed frozen and every
trade card showed STALE.

This patch changes quote transport only. Entry, exit, SL, sizing and strategy
logic remain unchanged.
"""
from __future__ import annotations

import sys
import threading
import time

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
                    return result
                return quote_fetcher(trade)

            state["open_quote_transport"] = "UPSTOX_BATCH_LTP_V1"
            return original(conn, user_id, rows, scans, fetch_from_batch, live_order, state)

        runtime._manage_rows = manage_rows_batched
        runtime.UPSTOX_OPEN_QUOTE_BATCH_PATCH = "V1"
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
