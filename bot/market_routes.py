import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Query

from database import get_db
from auth.routes import get_current_user
from auth.utils import decrypt_credential
from bot.angel_fetcher import (
    INDEX_EXCHANGE,
    INDEX_TOKENS,
    INDEX_TRADING_SYMBOLS,
    _get_ltp_session,
    _ltp_lock,
    _ltp_sessions,
    get_index_quotes,
)
from bot.brokers.factory import create_broker


router = APIRouter()

INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"]

ZERODHA_INDEX_TOKENS = {
    "NIFTY": 256265,
    "BANKNIFTY": 260105,
    "SENSEX": 265,
}
ZERODHA_INDEX_EXCHANGE = {
    "NIFTY": "NSE",
    "BANKNIFTY": "NSE",
    "SENSEX": "BSE",
}
UPSTOX_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}

# The chart polls one selected index once per second. Keep broker sessions alive
# and absorb duplicate requests inside the same second instead of logging in or
# hitting the broker repeatedly. This cache is display-only and is never used by
# the strategy entry engine.
QUOTE_CACHE_SECONDS = 0.80
_quote_cache = {}
_quote_cache_lock = threading.Lock()
_multi_sessions = {}
_multi_sessions_lock = threading.Lock()


def _get_active_broker(user_id):
    conn = get_db()
    cred = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    if not cred:
        return None, None
    creds = {
        "client_id": cred["client_id"],
        "api_key": decrypt_credential(cred["api_key"]),
        "password": decrypt_credential(cred["api_secret"]),
        "totp_secret": decrypt_credential(cred["totp_secret"])
        if cred["totp_secret"]
        else None,
    }
    return cred["broker_name"], creds


def _get_multi_session(user_id, broker_name, creds):
    key = (int(user_id), str(broker_name).lower())
    with _multi_sessions_lock:
        obj = _multi_sessions.get(key)
    if obj is not None:
        return obj

    obj = create_broker(
        broker_name,
        creds["client_id"],
        creds["api_key"],
        creds["password"],
        creds.get("totp_secret"),
    )
    login_result = obj.login()
    if not login_result.get("success"):
        raise RuntimeError(login_result.get("message", "Login failed"))

    with _multi_sessions_lock:
        _multi_sessions[key] = obj
    return obj


def _clear_broker_session(user_id, broker_name):
    if str(broker_name).lower() == "angelone":
        with _ltp_lock:
            _ltp_sessions.pop(int(user_id), None)
        return

    key = (int(user_id), str(broker_name).lower())
    with _multi_sessions_lock:
        _multi_sessions.pop(key, None)


def _fetch_one_quote(user_id, broker_name, creds, symbol):
    broker_name = str(broker_name or "").lower()

    if broker_name == "angelone":
        obj = _get_ltp_session(user_id, creds)
        quote = obj.ltpData(
            INDEX_EXCHANGE[symbol],
            INDEX_TRADING_SYMBOLS[symbol],
            INDEX_TOKENS[symbol],
        )
        if not isinstance(quote, dict) or not quote.get("status"):
            raise RuntimeError(
                (quote or {}).get("message", "Angel LTP unavailable")
                if isinstance(quote, dict)
                else "Angel LTP unavailable"
            )
        return float(quote["data"]["ltp"])

    obj = _get_multi_session(user_id, broker_name, creds)
    if broker_name == "zerodha":
        result = obj.get_ltp(
            symbol=ZERODHA_INDEX_TOKENS[symbol],
            exchange=ZERODHA_INDEX_EXCHANGE[symbol],
        )
    elif broker_name == "upstox":
        result = obj.get_ltp(symbol=UPSTOX_INDEX_KEYS[symbol])
    else:
        raise RuntimeError(f"Unsupported broker: {broker_name}")

    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError(
            (result or {}).get("message", "Index LTP unavailable")
            if isinstance(result, dict)
            else "Index LTP unavailable"
        )
    return float(result["ltp"])


def _get_quotes_multi(broker_name, creds):
    """LTP fetch for Zerodha/Upstox (non-Angel brokers)."""
    obj = create_broker(
        broker_name,
        creds["client_id"],
        creds["api_key"],
        creds["password"],
        creds.get("totp_secret"),
    )
    login_result = obj.login()
    if not login_result.get("success"):
        raise RuntimeError(login_result.get("message", "Login failed"))

    results = {}
    for symbol in INDEX_SYMBOLS:
        try:
            if broker_name == "zerodha":
                token = ZERODHA_INDEX_TOKENS[symbol]
                exch = ZERODHA_INDEX_EXCHANGE[symbol]
                r = obj.get_ltp(symbol=token, exchange=exch)
            elif broker_name == "upstox":
                key = UPSTOX_INDEX_KEYS[symbol]
                r = obj.get_ltp(symbol=key)
            else:
                r = {"success": False}

            if r.get("success"):
                results[symbol] = {
                    "ltp": r["ltp"],
                    "status": "connected",
                }
            else:
                results[symbol] = {
                    "ltp": None,
                    "status": "not_connected",
                    "error": r.get("message"),
                }
        except Exception as exc:
            results[symbol] = {
                "ltp": None,
                "status": "not_connected",
                "error": str(exc),
            }
    return results


@router.get("/market/quote")
def market_quote(
    instrument: str = Query("NIFTY"),
    authorization: str = Header(None),
):
    """One lightweight display quote for the currently selected chart.

    The mobile chart may call this once per second. The response only updates the
    visual in-progress candle; strategy decisions continue using completed
    one-minute candles in the bot runtime.
    """
    user = get_current_user(authorization)
    symbol = str(instrument or "NIFTY").upper().strip()
    if symbol not in INDEX_SYMBOLS:
        return {
            "success": False,
            "instrument": symbol,
            "message": "Unsupported index",
        }

    cache_key = (int(user["id"]), symbol)
    now_monotonic = time.monotonic()
    with _quote_cache_lock:
        cached = _quote_cache.get(cache_key)
        if cached and now_monotonic - cached["stored_at"] < QUOTE_CACHE_SECONDS:
            return {
                **cached["payload"],
                "cache_hit": True,
            }

    broker_name, creds = _get_active_broker(user["id"])
    if not creds:
        return {
            "success": False,
            "instrument": symbol,
            "message": "Active broker not connected",
            "as_of": datetime.now(timezone.utc).isoformat(),
        }

    try:
        ltp = _fetch_one_quote(user["id"], broker_name, creds, symbol)
        payload = {
            "success": True,
            "instrument": symbol,
            "ltp": round(float(ltp), 2),
            "source": broker_name,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "display_only": True,
            "strategy_uses_completed_one_minute_candle": True,
            "cache_hit": False,
        }
        with _quote_cache_lock:
            _quote_cache[cache_key] = {
                "stored_at": now_monotonic,
                "payload": payload,
            }
        return payload
    except Exception as exc:
        _clear_broker_session(user["id"], broker_name)
        return {
            "success": False,
            "instrument": symbol,
            "source": broker_name,
            "message": str(exc)[:160],
            "as_of": datetime.now(timezone.utc).isoformat(),
        }


@router.get("/market/status")
def market_status(authorization: str = Header(None)):
    """Returns live market feed status using the user's active broker."""
    user = get_current_user(authorization)

    feed_connected = False
    source = "not_connected"
    indices_data = []
    quotes = {}

    broker_name, creds = _get_active_broker(user["id"])
    if creds:
        try:
            if broker_name == "angelone":
                quotes = get_index_quotes(user["id"], creds)
            else:
                quotes = _get_quotes_multi(broker_name, creds)
            source = broker_name
        except Exception:
            quotes = {}

    for symbol in INDEX_SYMBOLS:
        q = quotes.get(symbol, {})
        status = q.get("status", "not_connected")
        if status == "connected":
            feed_connected = True
        indices_data.append(
            {
                "symbol": symbol,
                "ltp": q.get("ltp"),
                "change": None,
                "change_percent": None,
                "status": status,
                "error": q.get("error"),
            }
        )

    message = "Live feed connected" if feed_connected else "Live feed not connected"

    return {
        "success": True,
        "feed_connected": feed_connected,
        "source": source,
        "message": message,
        "indices": indices_data,
    }
