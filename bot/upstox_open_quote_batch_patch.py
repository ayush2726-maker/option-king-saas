"""Batch Upstox OPEN-position LTP reads per monitor cycle.

Avoid one market-quote request per open trade. Upstox returns batch quote keys
as full instrument keys (for example NSE_FO|12345), while paper_trades may store
only the numeric token. Normalize both forms so a successful batch response is
actually matched back to the trade. Successful quotes stamp quote_updated_at
immediately. Entry, exit, SL, sizing and strategy logic remain unchanged.
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


def _full_key(obj, ref, exchange):
    try:
        return str(obj._quote_instrument(ref, exchange))
    except Exception:
        raw = str(ref or "").strip()
        if "|" in raw:
            return raw
        segment = "BSE_FO" if str(exchange or "").upper().startswith(("BSE", "BFO")) else "NSE_FO"
        return f"{segment}|{raw}"


def _find_quote(obj, quotes, ref, exchange):
    """Match Upstox batch output regardless of response key representation."""
    raw = str(ref or "").strip()
    full = _full_key(obj, raw, exchange)

    # Fast exact forms first.
    for key in (raw, full, full.replace("|", ":", 1)):
        quote = quotes.get(key)
        if isinstance(quote, dict) and quote.get("success"):
            return quote

    # Upstox can key by trading symbol while returning instrument_token inside
    # the quote. Match that token as the authoritative fallback.
    raw_tail = raw.split("|", 1)[-1]
    full_tail = full.split("|", 1)[-1]
    for key, quote in quotes.items():
        if not isinstance(quote, dict) or not quote.get("success"):
            continue
        returned = str(quote.get("instrument_token") or "").strip()
        key_norm = str(key or "").replace(":", "|", 1)
        key_tail = key_norm.split("|", 1)[-1]
        if returned in {raw, raw_tail, full, full_tail} or key_tail in {raw, raw_tail, full_tail}:
            return quote
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
            quote = _find_quote(obj, quotes, ref, exchange)
            if quote:
                output[trade["id"]] = dict(quote)
            else:
                output[trade["id"]] = {
                    "success": False,
                    "message": f"No batched LTP data for {ref}; keys={list(quotes)[:4]}",
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
                str(result.get("quote_source") or "UPSTOX_BATCH_LTP_V3"),
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

            state["open_quote_transport"] = "UPSTOX_BATCH_LTP_V3_KEYFIX"
            return original(conn, user_id, rows, scans, fetch_from_batch, live_order, state)

        runtime._manage_rows = manage_rows_batched
        runtime.UPSTOX_OPEN_QUOTE_BATCH_PATCH = "V3_KEYFIX"
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
