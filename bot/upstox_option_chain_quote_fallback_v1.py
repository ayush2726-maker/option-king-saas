"""Fallback stale OPEN PAPER option quotes through Upstox option-chain data.

Primary market-quote LTP remains unchanged. This watcher only runs when a paper
option has been stale for several seconds and uses the official option-chain API,
which returns strike-level CE/PE market_data.ltp. Existing exit/trailing logic is
then evaluated through paper_quote_authority_v2._apply_quote.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone

import requests

from database import get_db

_LOOP_SECONDS = 4.0
_TAKEOVER_SECONDS = 12.0
_MIN_ATTEMPT_SECONDS = 12.0
_started = False
_lock = threading.Lock()
_last_attempt = {}

_UNDERLYING_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}

_SYMBOL_RE = re.compile(
    r"\b(NIFTY|BANKNIFTY|SENSEX)\s+(\d+(?:\.\d+)?)\s+(CE|PE)\s+(\d{1,2})\s+([A-Z]{3})\s+(\d{2,4})\b",
    re.I,
)
_MONTH = {m: i for i, m in enumerate(("JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"), 1)}


def _value(row, key, default=None):
    try:
        v = row[key]
        return default if v is None else v
    except Exception:
        return default


def _age(row):
    raw = _value(row, "quote_updated_at")
    if not raw:
        return float("inf")
    try:
        text = str(raw).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return float("inf")


def _parse_contract(symbol):
    m = _SYMBOL_RE.search(str(symbol or "").upper())
    if not m:
        return None
    underlying, strike, side, day, mon, year = m.groups()
    year = int(year)
    if year < 100:
        year += 2000
    month = _MONTH.get(mon.upper())
    if not month:
        return None
    try:
        expiry = f"{year:04d}-{month:02d}-{int(day):02d}"
    except Exception:
        return None
    return underlying.upper(), float(strike), side.upper(), expiry


def _upstox_session(user_id):
    try:
        from bot.paper_quote_authority_v2 import _candidate_rows, _login_cached
        for cred in _candidate_rows(int(user_id)):
            if str(_value(cred, "broker_name", "")).lower() != "upstox":
                continue
            broker, obj, _key = _login_cached(cred)
            if str(broker).lower() == "upstox":
                return obj
    except Exception:
        return None
    return None


def _fetch_chain(obj, underlying, expiry):
    key = _UNDERLYING_KEYS.get(underlying)
    if not key:
        return None, "UNSUPPORTED_UNDERLYING"
    try:
        response = requests.get(
            f"{obj.BASE_URL}/option/chain",
            params={"instrument_key": key, "expiry_date": expiry},
            headers=obj._h(),
            timeout=10,
        )
        payload = response.json()
    except Exception as exc:
        return None, f"OPTION_CHAIN_REQUEST:{type(exc).__name__}:{str(exc)[:120]}"
    if response.status_code != 200 or str(payload.get("status") or "").lower() != "success":
        return None, f"OPTION_CHAIN_HTTP_{response.status_code}:{str(payload)[:180]}"
    return payload.get("data") or [], None


def _chain_ltp(chain, strike, side):
    best = None
    for row in chain or []:
        try:
            row_strike = float(row.get("strike_price"))
        except Exception:
            continue
        if abs(row_strike - float(strike)) > 0.001:
            continue
        leg = row.get("call_options" if side == "CE" else "put_options") or {}
        market = leg.get("market_data") or {}
        try:
            ltp = float(market.get("ltp") or 0)
        except Exception:
            ltp = 0.0
        if ltp > 0:
            best = (ltp, str(leg.get("instrument_key") or ""))
            break
    return best


def _record_error(conn, trade_id, message):
    try:
        conn.execute(
            """UPDATE paper_trades SET quote_failed_at=?, quote_error=?,
               quote_failure_count=COALESCE(quote_failure_count,0)+1
               WHERE id=? AND status='OPEN'""",
            (datetime.now(timezone.utc).isoformat(), "OPTION_CHAIN_FALLBACK:" + str(message)[:260], int(trade_id)),
        )
        conn.commit()
    except Exception:
        pass


def _recover_user(user_id, trades):
    now = time.monotonic()
    if now - _last_attempt.get(int(user_id), 0.0) < _MIN_ATTEMPT_SECONDS:
        return
    _last_attempt[int(user_id)] = now

    obj = _upstox_session(user_id)
    if obj is None:
        return

    parsed = {}
    groups = {}
    for trade in trades:
        contract = _parse_contract(_value(trade, "symbol"))
        if not contract:
            continue
        parsed[int(trade["id"])] = contract
        groups.setdefault((contract[0], contract[3]), []).append(trade)
    if not groups:
        return

    from bot.paper_quote_authority_v2 import _apply_quote
    from bot import auto_portfolio_runtime as runtime
    conn = get_db()
    try:
        runtime._ensure_schema(conn)
        for (underlying, expiry), rows in groups.items():
            chain, error = _fetch_chain(obj, underlying, expiry)
            if error:
                for trade in rows:
                    _record_error(conn, trade["id"], error)
                continue
            for trade in rows:
                current = conn.execute(
                    "SELECT * FROM paper_trades WHERE id=? AND status='OPEN'",
                    (trade["id"],),
                ).fetchone()
                if not current or _age(current) <= _TAKEOVER_SECONDS:
                    continue
                _u, strike, side, _e = parsed[int(trade["id"])]
                hit = _chain_ltp(chain, strike, side)
                if not hit:
                    _record_error(conn, trade["id"], f"NO_STRIKE_LTP:{underlying}:{strike}:{side}:{expiry}")
                    continue
                ltp, instrument_key = hit
                try:
                    if _apply_quote(conn, runtime, int(user_id), int(trade["id"]), ltp, "upstox_option_chain"):
                        conn.execute(
                            """UPDATE paper_trades SET token=COALESCE(NULLIF(?,''),token),
                               quote_source='UPSTOX_OPTION_CHAIN_FALLBACK_V1'
                               WHERE id=? AND status='OPEN'""",
                            (instrument_key, int(trade["id"])),
                        )
                        conn.commit()
                except Exception as exc:
                    _record_error(conn, trade["id"], f"APPLY:{type(exc).__name__}:{str(exc)[:120]}")
    finally:
        conn.close()


def _loop():
    while True:
        try:
            conn = get_db()
            try:
                rows = conn.execute(
                    """SELECT * FROM paper_trades WHERE status='OPEN'
                       AND LOWER(COALESCE(trading_mode,'paper'))='paper'"""
                ).fetchall()
            finally:
                conn.close()
            grouped = {}
            for row in rows:
                if _age(row) > _TAKEOVER_SECONDS:
                    grouped.setdefault(int(row["user_id"]), []).append(row)
            for user_id, trades in grouped.items():
                try:
                    _recover_user(user_id, trades)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(_LOOP_SECONDS)


def schedule_upstox_option_chain_quote_fallback():
    global _started
    if _started:
        return
    with _lock:
        if _started:
            return
        threading.Thread(target=_loop, name="okai-upstox-option-chain-ltp-fallback", daemon=True).start()
        _started = True
