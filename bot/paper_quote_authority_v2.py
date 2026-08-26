"""Authoritative failover quote monitor for OPEN PAPER trades.

The normal AUTO runtime remains the primary quote/strategy loop. This monitor
only takes over when an OPEN PAPER trade has not received a successful quote for
several seconds. It batches Upstox quotes, uses the same PAPER broker fallback
chain, stamps freshness at the point of successful market data, and runs the
existing exit/trailing evaluation on that fresh LTP.

It also disables the two older polling recovery loops once loaded. Those loops
were independently calling broker quote APIs every 20/30 seconds while the AUTO
runtime and Upstox batch path were also active. Multiple overlapping pollers can
create throttling and make a healthy trade repeatedly flip to STALE.

No entry, score, strategy, sizing, SL formula, profit-lock rule or live-order
rule is changed here.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from database import get_db

_LOOP_SECONDS = 2.0
_TAKEOVER_SECONDS = 8.0
_MIN_USER_ATTEMPT_SECONDS = 8.0
_SESSION_TTL_SECONDS = 300.0

_started = False
_lock = threading.Lock()
_last_attempt: dict[int, float] = {}
_session_cache: dict[tuple, tuple[float, str, object]] = {}
_broker_backoff_until: dict[tuple, float] = {}


def _value(row, key, default=None):
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        if isinstance(row, dict):
            value = row.get(key)
            return default if value is None else value
        return default


def _parse_utc(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _quote_age(row):
    parsed = _parse_utc(_value(row, "quote_updated_at"))
    if parsed is None:
        return float("inf")
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _needs_takeover(row):
    return _quote_age(row) > _TAKEOVER_SECONDS


def _disable_legacy_pollers():
    """Stop duplicate stale-quote polling while keeping their code available."""
    try:
        from bot import direct_stale_quote_recovery_v1 as direct
        direct._stale = lambda row: False
        direct.PAPER_QUOTE_AUTHORITY_SUPERSEDED = "V2"
    except Exception:
        pass
    try:
        from bot import paper_quote_multi_broker_recovery_v1 as multi
        multi._stale = lambda row: False
        multi.PAPER_QUOTE_AUTHORITY_SUPERSEDED = "V2"
    except Exception:
        pass


def _candidate_rows(user_id):
    try:
        from bot.paper_quote_multi_broker_recovery_v1 import _candidate_rows as existing
        return list(existing(int(user_id)) or [])
    except Exception:
        return []


def _credential_key(row):
    return (
        int(_value(row, "id", 0) or 0),
        str(_value(row, "broker_name", "") or "").lower(),
        str(_value(row, "client_id", "") or ""),
    )


def _login_cached(row):
    key = _credential_key(row)
    now = time.monotonic()
    cached = _session_cache.get(key)
    if cached and now - cached[0] < _SESSION_TTL_SECONDS:
        return cached[1], cached[2], key

    from bot.paper_quote_multi_broker_recovery_v1 import _login
    broker, obj = _login(row)
    _session_cache[key] = (now, broker, obj)
    return broker, obj, key


def _full_ref(obj, ref, exchange):
    text = str(ref or "").strip()
    if not text:
        return ""
    if "|" in text:
        return text
    try:
        builder = getattr(obj, "_quote_instrument", None)
        if callable(builder):
            return str(builder(text, exchange))
    except Exception:
        pass
    segment = "BSE_FO" if str(exchange or "").upper().startswith(("BSE", "BFO")) else "NSE_FO"
    return f"{segment}|{text}"


def _match_upstox_quote(obj, quotes, ref, exchange):
    if not isinstance(quotes, dict):
        return None
    bare = str(ref or "").strip()
    full = _full_ref(obj, bare, exchange)
    candidates = [bare, full]
    for candidate in candidates:
        quote = quotes.get(candidate)
        if isinstance(quote, dict) and quote.get("success"):
            return quote
    for key, quote in quotes.items():
        if not isinstance(quote, dict) or not quote.get("success"):
            continue
        returned_token = str(quote.get("instrument_token") or "").strip()
        normalized_key = str(key or "").replace(":", "|", 1)
        if returned_token and returned_token in {bare, full}:
            return quote
        if normalized_key in {bare, full}:
            return quote
        if bare and (normalized_key.endswith("|" + bare) or returned_token.endswith("|" + bare)):
            return quote
    return None


def _upstox_batch(obj, rows):
    results = {}
    by_exchange = {}
    refs_by_trade = {}
    for trade in rows:
        exchange = str(_value(trade, "exch_seg", "NSE_FO") or "NSE_FO")
        refs = []
        for ref in (_value(trade, "token"), _value(trade, "symbol")):
            text = str(ref or "").strip()
            if text and text not in refs:
                refs.append(text)
        refs_by_trade[int(trade["id"])] = (exchange, refs)
        if refs:
            # Token/instrument-key first. Symbol remains an exact fallback below.
            by_exchange.setdefault(exchange, []).append(refs[0])

    for exchange, refs in by_exchange.items():
        unique = list(dict.fromkeys(refs))
        try:
            response = obj.get_ltps(unique, exchange=exchange) or {}
        except Exception as exc:
            response = {"success": False, "message": str(exc)}

        if not response.get("success"):
            retry = response.get("retry_after_seconds") if response.get("rate_limited") else None
            message = str(response.get("message") or "UPSTOX_BATCH_LTP_FAILED")
            for trade_id, (trade_exchange, _refs) in refs_by_trade.items():
                if trade_exchange == exchange:
                    results[trade_id] = {
                        "success": False,
                        "message": message,
                        "rate_limited": bool(response.get("rate_limited")),
                        "retry_after_seconds": retry,
                    }
            continue

        quotes = response.get("quotes") or {}
        for trade_id, (trade_exchange, trade_refs) in refs_by_trade.items():
            if trade_exchange != exchange or not trade_refs:
                continue
            quote = _match_upstox_quote(obj, quotes, trade_refs[0], exchange)
            if quote is None and len(trade_refs) > 1:
                # Symbol fallback uses one request only when the batched token did
                # not map, rather than for every healthy trade on every cycle.
                try:
                    one = obj.get_ltp(trade_refs[1], exchange=exchange) or {}
                except Exception as exc:
                    one = {"success": False, "message": str(exc)}
                quote = one if one.get("success") else None
                if quote is None:
                    results[trade_id] = one
                    continue
            if quote is not None:
                results[trade_id] = dict(quote)
            else:
                results[trade_id] = {
                    "success": False,
                    "message": f"No Upstox quote mapping for {trade_refs[0]}",
                }
    return results


def _other_quotes(obj, broker, rows):
    results = {}
    from bot.paper_quote_multi_broker_recovery_v1 import _quote
    for trade in rows:
        try:
            ltp = float(_quote(obj, broker, trade))
            results[int(trade["id"])] = {"success": ltp > 0, "ltp": ltp}
        except Exception as exc:
            results[int(trade["id"])] = {"success": False, "message": str(exc)}
    return results


def _apply_quote(conn, runtime, user_id, trade_id, ltp, broker):
    current = conn.execute(
        "SELECT * FROM paper_trades WHERE id=? AND user_id=? AND status='OPEN'",
        (int(trade_id), int(user_id)),
    ).fetchone()
    if not current:
        return True
    if str(_value(current, "trading_mode", "paper") or "paper").lower() != "paper":
        return True

    ltp = float(ltp or 0)
    if ltp <= 0:
        return False

    evaluation = runtime._evaluate_exit(current, ltp, None, None)
    runtime._update_open(conn, current, ltp, evaluation)
    if evaluation.get("reason"):
        runtime._close(conn, int(user_id), current, ltp, evaluation["reason"])
        return True

    # Freshness authority is written explicitly after all existing wrappers.
    # This makes the API status independent of runtime monkey-patch order.
    conn.execute(
        """
        UPDATE paper_trades
        SET last_ltp=?, quote_updated_at=?, quote_source=?,
            quote_failed_at=NULL, quote_error=NULL, quote_failure_count=0
        WHERE id=? AND status='OPEN'
        """,
        (
            ltp,
            datetime.now(timezone.utc).isoformat(),
            f"{str(broker).upper()}_PAPER_QUOTE_AUTHORITY_V2",
            int(trade_id),
        ),
    )
    conn.commit()
    return True


def _record_failure(conn, trade_id, message):
    try:
        conn.execute(
            """
            UPDATE paper_trades
            SET quote_failed_at=?, quote_error=?,
                quote_failure_count=COALESCE(quote_failure_count,0)+1
            WHERE id=? AND status='OPEN'
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                "QUOTE_AUTHORITY_V2:" + str(message or "QUOTE_FAILED")[:280],
                int(trade_id),
            ),
        )
        conn.commit()
    except Exception:
        pass


def _recover_user(user_id, rows):
    now = time.monotonic()
    if now - _last_attempt.get(int(user_id), 0.0) < _MIN_USER_ATTEMPT_SECONDS:
        return
    _last_attempt[int(user_id)] = now

    pending = {int(row["id"]): row for row in rows if _needs_takeover(row)}
    if not pending:
        return

    candidates = _candidate_rows(user_id)
    if not candidates:
        return

    from bot import auto_portfolio_runtime as runtime
    conn = get_db()
    try:
        runtime._ensure_schema(conn)
        errors = {trade_id: [] for trade_id in pending}

        for credential in candidates:
            if not pending:
                break
            key = _credential_key(credential)
            if time.monotonic() < _broker_backoff_until.get(key, 0.0):
                continue
            try:
                broker, obj, key = _login_cached(credential)
            except Exception as exc:
                for trade_id in pending:
                    errors[trade_id].append("LOGIN:" + str(exc)[:120])
                continue

            active_rows = list(pending.values())
            if str(broker).lower() == "upstox" and callable(getattr(obj, "get_ltps", None)):
                quotes = _upstox_batch(obj, active_rows)
            else:
                quotes = _other_quotes(obj, broker, active_rows)

            succeeded = []
            for trade_id, trade in list(pending.items()):
                result = quotes.get(trade_id) or {}
                if result.get("rate_limited"):
                    retry = result.get("retry_after_seconds") or 45
                    try:
                        retry = max(5.0, float(retry))
                    except Exception:
                        retry = 45.0
                    _broker_backoff_until[key] = time.monotonic() + retry
                if result.get("success"):
                    try:
                        if _apply_quote(conn, runtime, user_id, trade_id, result.get("ltp"), broker):
                            succeeded.append(trade_id)
                            continue
                    except Exception as exc:
                        errors[trade_id].append("APPLY:" + str(exc)[:120])
                errors[trade_id].append(str(result.get("message") or "QUOTE_FAILED")[:140])

            for trade_id in succeeded:
                pending.pop(trade_id, None)

        # Only write a failure after all configured PAPER data sources failed.
        for trade_id in pending:
            _record_failure(conn, trade_id, " | ".join(errors.get(trade_id, [])[-4:]))
    finally:
        conn.close()


def _loop():
    _disable_legacy_pollers()
    while True:
        try:
            # Re-assert in case an older module was imported after startup.
            _disable_legacy_pollers()
            conn = get_db()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM paper_trades
                    WHERE status='OPEN'
                      AND LOWER(COALESCE(trading_mode,'paper'))='paper'
                    """
                ).fetchall()
            finally:
                conn.close()

            grouped = {}
            for row in rows:
                if _needs_takeover(row):
                    grouped.setdefault(int(row["user_id"]), []).append(row)
            for user_id, stale_rows in grouped.items():
                try:
                    _recover_user(user_id, stale_rows)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(_LOOP_SECONDS)


def schedule_paper_quote_authority_v2():
    global _started
    if _started:
        return
    with _lock:
        if _started:
            return
        _disable_legacy_pollers()
        threading.Thread(
            target=_loop,
            name="okai-paper-quote-authority-v2",
            daemon=True,
        ).start()
        _started = True
