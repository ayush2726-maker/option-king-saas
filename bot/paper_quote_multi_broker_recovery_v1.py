"""Recover stale PAPER quotes across the effective data-broker chain.

Why this exists:
- PAPER can use the user's broker or the shared owner broker as market-data source.
- A trade may remain open while the selected/active broker changes.
- The older direct recovery used only the user's latest active broker, which can
  leave an older PAPER trade stale even though another configured data broker can
  still quote the contract.

This module changes quote transport only. It does not alter entries, exits,
position sizing, SL math, or live order execution.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from auth.utils import decrypt_credential
from database import get_db

_INTERVAL = 20
_STALE_SECONDS = 20
_started = False
_lock = threading.Lock()
_last_attempt = {}


def _row_value(row, key, default=None):
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        return default


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
    dt = _parse(_row_value(row, "quote_updated_at"))
    return dt is None or (datetime.now(timezone.utc) - dt).total_seconds() > _STALE_SECONDS


def _creds(row):
    return {
        "client_id": row["client_id"],
        "api_key": decrypt_credential(row["api_key"]),
        "password": decrypt_credential(row["api_secret"]),
        "totp_secret": decrypt_credential(row["totp_secret"]) if _row_value(row, "totp_secret") else None,
    }


def _login(row):
    broker = str(_row_value(row, "broker_name", "angelone") or "angelone").lower()
    creds = _creds(row)
    if broker == "angelone":
        from bot.angel_fetcher import angel_login
        return broker, angel_login(creds)
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
    return broker, obj


def _quote(obj, broker, trade):
    if broker == "angelone":
        errors = []

        # Fast path for trades originally opened with Angel identifiers.
        try:
            q = obj.ltpData(
                trade["exch_seg"],
                trade["symbol"],
                trade["token"],
            )
            ltp = float((q.get("data") or {}).get("ltp") or 0)
            if ltp > 0:
                return ltp
            errors.append(str(q.get("message") or "ANGEL_DIRECT_LTP_EMPTY"))
        except Exception as exc:
            errors.append(str(exc))

        # A PAPER trade may have been opened while the shared feed was Upstox.
        # Its stored token/symbol cannot be sent to Angel. Resolve the exact
        # canonical contract—never a nearby strike/expiry—and retry Angel LTP.
        try:
            from bot.option_chain import resolve_exact_option

            resolved = resolve_exact_option(
                _row_value(trade, "underlying"),
                _row_value(trade, "expiry"),
                _row_value(trade, "strike"),
                _row_value(trade, "side"),
            )
            if not resolved:
                raise RuntimeError("ANGEL_EXACT_CONTRACT_NOT_FOUND")
            q = obj.ltpData(
                resolved["exch_seg"],
                resolved["symbol"],
                resolved["token"],
            )
            ltp = float((q.get("data") or {}).get("ltp") or 0)
            if ltp > 0:
                return ltp
            errors.append(str(q.get("message") or "ANGEL_RESOLVED_LTP_EMPTY"))
        except Exception as exc:
            errors.append(str(exc))

        raise RuntimeError(" | ".join(errors[-3:]) or "ANGEL_LTP_FAILED")

    exchange = _row_value(trade, "exch_seg") or ("NSE_FO" if broker == "upstox" else "NFO")
    refs = []
    for ref in (_row_value(trade, "token"), _row_value(trade, "symbol")):
        if ref and str(ref) not in refs:
            refs.append(str(ref))
    errors = []
    for ref in refs:
        try:
            result = obj.get_ltp(ref, exchange=exchange) or {}
            if result.get("success") and float(result.get("ltp") or 0) > 0:
                return float(result["ltp"])
            errors.append(str(result.get("message") or "LTP_FAILED"))
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError(" | ".join(errors[-3:]) or "LTP_FAILED")


def _candidate_rows(user_id):
    """Return data-broker rows in the same preference order PAPER uses."""
    candidates = []
    seen = set()
    try:
        from bot.shared_nonlive_data_feed_patch import _effective_data_broker, _owner_broker
        row, _source = _effective_data_broker(int(user_id), owner_first=False)
        if row:
            key = int(_row_value(row, "id", 0) or 0)
            if key not in seen:
                candidates.append(row)
                seen.add(key)
        conn = get_db()
        try:
            owner = _owner_broker(conn, int(user_id))
        finally:
            conn.close()
        if owner:
            key = int(_row_value(owner, "id", 0) or 0)
            if key not in seen:
                candidates.append(owner)
                seen.add(key)
    except Exception:
        pass

    # Last fallback: any active personal credential row.
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC,id DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
    finally:
        conn.close()
    if row:
        key = int(_row_value(row, "id", 0) or 0)
        if key not in seen:
            candidates.append(row)
    return candidates


def _recover_user(user_id, rows):
    now = time.monotonic()
    if now - _last_attempt.get(int(user_id), 0.0) < _INTERVAL:
        return
    _last_attempt[int(user_id)] = now

    candidates = _candidate_rows(user_id)
    if not candidates:
        return

    sessions = []
    errors = []
    for row in candidates:
        try:
            sessions.append(_login(row))
        except Exception as exc:
            errors.append(f"LOGIN:{str(exc)[:120]}")

    if not sessions:
        return

    from bot import auto_portfolio_runtime as runtime
    conn = get_db()
    try:
        runtime._ensure_schema(conn)
        for trade in rows:
            current = conn.execute(
                "SELECT * FROM paper_trades WHERE id=? AND status='OPEN'",
                (trade["id"],),
            ).fetchone()
            if not current or not _stale(current):
                continue
            if str(_row_value(current, "trading_mode", "paper") or "paper").lower() != "paper":
                continue

            success = False
            local_errors = []
            for broker, obj in sessions:
                try:
                    ltp = _quote(obj, broker, current)
                    if ltp <= 0:
                        raise RuntimeError("INVALID_OPTION_LTP")
                    evaluation = runtime._evaluate_exit(current, ltp, None, None)
                    runtime._update_open(conn, current, ltp, evaluation)
                    if evaluation.get("reason"):
                        runtime._close(conn, int(user_id), current, ltp, evaluation["reason"])
                    else:
                        conn.execute(
                            """UPDATE paper_trades
                               SET quote_updated_at=?, quote_source=?, quote_failed_at=NULL,
                                   quote_error=NULL, quote_failure_count=0
                               WHERE id=? AND status='OPEN'""",
                            (
                                datetime.now(timezone.utc).isoformat(),
                                f"{broker.upper()}_PAPER_MULTI_RECOVERY",
                                current["id"],
                            ),
                        )
                        conn.commit()
                    success = True
                    break
                except Exception as exc:
                    local_errors.append(f"{broker}:{str(exc)[:140]}")

            if not success:
                conn.execute(
                    """UPDATE paper_trades
                       SET quote_failed_at=?, quote_error=?,
                           quote_failure_count=COALESCE(quote_failure_count,0)+1
                       WHERE id=? AND status='OPEN'""",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        "PAPER_MULTI:" + " | ".join(local_errors[-3:] + errors[-1:])[:300],
                        current["id"],
                    ),
                )
                conn.commit()
    finally:
        conn.close()


def _loop():
    while True:
        try:
            conn = get_db()
            try:
                rows = conn.execute(
                    "SELECT * FROM paper_trades WHERE status='OPEN' AND LOWER(COALESCE(trading_mode,'paper'))='paper'"
                ).fetchall()
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


def schedule_paper_quote_multi_broker_recovery():
    global _started
    if _started:
        return
    with _lock:
        if _started:
            return
        threading.Thread(
            target=_loop,
            name="okai-paper-multi-broker-quote-recovery",
            daemon=True,
        ).start()
        _started = True
