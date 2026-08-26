"""Directly recover stale OPEN option quotes using a fresh broker session.

This is a last-resort quote path only for rows whose runtime quote timestamp is
stale. It is independent of the long-running AUTO runtime session, so a wedged
session cannot leave paper P&L/SL frozen indefinitely.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from auth.utils import decrypt_credential
from database import get_db

_INTERVAL = 5
_STALE_SECONDS = 10
_started = False
_lock = threading.Lock()


def _parse(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _stale(row):
    try:
        dt = _parse(row["quote_updated_at"])
    except Exception:
        dt = None
    return dt is None or (datetime.now(timezone.utc) - dt).total_seconds() > _STALE_SECONDS


def _credentials(conn, user_id):
    row = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
        (int(user_id),),
    ).fetchone()
    if not row:
        return None, None
    broker = str(row["broker_name"] or "angelone").lower()
    creds = {
        "api_key": decrypt_credential(row["api_key"]),
        "client_id": row["client_id"],
        "password": decrypt_credential(row["api_secret"]),
        "totp_secret": decrypt_credential(row["totp_secret"]) if row["totp_secret"] else None,
    }
    return broker, creds


def _fresh_session(broker, creds):
    if broker == "angelone":
        from bot.angel_fetcher import angel_login
        return angel_login(creds)
    from bot.brokers.factory import create_broker
    obj = create_broker(
        broker,
        creds["client_id"],
        creds["api_key"],
        creds["password"],
        creds.get("totp_secret"),
    )
    result = obj.login()
    if isinstance(result, dict) and not result.get("success", False):
        raise RuntimeError(result.get("message") or "BROKER_LOGIN_FAILED")
    return obj


def _quote(obj, broker, trade):
    if broker == "angelone":
        q = obj.ltpData(trade["exch_seg"], trade["symbol"], trade["token"])
        return float(q["data"]["ltp"])

    exchange = trade["exch_seg"] or ("NSE_FO" if broker == "upstox" else "NFO")
    refs = []
    for ref in (trade["token"], trade["symbol"]):
        if ref and ref not in refs:
            refs.append(ref)
    errors = []
    for ref in refs:
        try:
            result = obj.get_ltp(ref, exchange=exchange)
            if result.get("success") and float(result.get("ltp") or 0) > 0:
                return float(result["ltp"])
            errors.append(str(result.get("message") or "LTP_FAILED"))
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError(" | ".join(errors[-3:]) or "LTP_FAILED")


def _recover_user(user_id, rows):
    conn = get_db()
    try:
        broker, creds = _credentials(conn, user_id)
    finally:
        conn.close()
    if not broker or not creds:
        return

    try:
        obj = _fresh_session(broker, creds)
    except Exception as exc:
        conn = get_db()
        try:
            for trade in rows:
                conn.execute(
                    "UPDATE paper_trades SET quote_failed_at=?, quote_error=?, quote_failure_count=COALESCE(quote_failure_count,0)+1 WHERE id=? AND status='OPEN'",
                    (datetime.now(timezone.utc).isoformat(), f"DIRECT_LOGIN:{str(exc)[:300]}", trade["id"]),
                )
            conn.commit()
        finally:
            conn.close()
        return

    from bot import auto_portfolio_runtime as runtime
    conn = get_db()
    try:
        runtime._ensure_schema(conn)
        for trade in rows:
            current = conn.execute("SELECT * FROM paper_trades WHERE id=? AND status='OPEN'", (trade["id"],)).fetchone()
            if not current or not _stale(current):
                continue
            try:
                ltp = _quote(obj, broker, current)
                if ltp <= 0:
                    raise RuntimeError("INVALID_OPTION_LTP")
                evaluation = runtime._evaluate_exit(current, ltp, None, None)
                runtime._update_open(conn, current, ltp, evaluation)
                if evaluation.get("reason") and str(current["trading_mode"] or "paper").lower() == "paper":
                    runtime._close(conn, user_id, current, ltp, evaluation["reason"])
                else:
                    conn.execute(
                        "UPDATE paper_trades SET quote_updated_at=?, quote_source=?, quote_failed_at=NULL, quote_error=NULL, quote_failure_count=0 WHERE id=? AND status='OPEN'",
                        (datetime.now(timezone.utc).isoformat(), f"{broker.upper()}_DIRECT_RECOVERY", current["id"]),
                    )
                    conn.commit()
            except Exception as exc:
                conn.execute(
                    "UPDATE paper_trades SET quote_failed_at=?, quote_error=?, quote_failure_count=COALESCE(quote_failure_count,0)+1 WHERE id=? AND status='OPEN'",
                    (datetime.now(timezone.utc).isoformat(), f"DIRECT_QUOTE:{str(exc)[:300]}", current["id"]),
                )
                conn.commit()
    finally:
        conn.close()


def _loop():
    while True:
        try:
            conn = get_db()
            try:
                rows = conn.execute("SELECT * FROM paper_trades WHERE status='OPEN'").fetchall()
            finally:
                conn.close()
            grouped = {}
            for row in rows:
                if _stale(row):
                    grouped.setdefault(int(row["user_id"]), []).append(row)
            for user_id, stale_rows in grouped.items():
                try:
                    _recover_user(user_id, stale_rows)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(_INTERVAL)


def schedule_direct_stale_quote_recovery():
    global _started
    if _started:
        return
    with _lock:
        if _started:
            return
        threading.Thread(target=_loop, name="okai-direct-stale-quote-recovery", daemon=True).start()
        _started = True
