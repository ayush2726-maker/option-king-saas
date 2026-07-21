from datetime import datetime, timezone, timedelta
import threading
import time

from auth.utils import decrypt_credential
from bot.angel_fetcher import INDEX_EXCHANGE, INDEX_TOKENS
from bot.brokers.factory import create_broker
from database import get_db


UPSTOX_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}

ZERODHA_INDEX_NAMES = {
    "NIFTY": ("NSE", {"NIFTY 50", "NIFTY"}),
    "BANKNIFTY": ("NSE", {"NIFTY BANK", "BANKNIFTY"}),
    "SENSEX": ("BSE", {"SENSEX"}),
}

_CACHE = {}
_ERROR_CACHE = {}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_BROKER_THROTTLE_LOCK = threading.Lock()
_LAST_BROKER_REQUEST = {}
_ZERODHA_INDEX_TOKEN_CACHE = {}


def _lock_for(key):
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def _cache_ttl(days):
    return 75 if int(days) == 1 else 600


def _throttle(broker_name):
    minimum_gap = 1.05 if broker_name == "angelone" else 0.25
    with _BROKER_THROTTLE_LOCK:
        now = time.monotonic()
        last = float(_LAST_BROKER_REQUEST.get(broker_name, 0) or 0)
        wait_for = minimum_gap - (now - last)
        if wait_for > 0:
            time.sleep(wait_for)
        _LAST_BROKER_REQUEST[broker_name] = time.monotonic()


def _normalise_rows(rows):
    output = []
    for row in rows or []:
        if isinstance(row, dict):
            values = [
                row.get("date") or row.get("time") or row.get("timestamp"),
                row.get("open"),
                row.get("high"),
                row.get("low"),
                row.get("close"),
                row.get("volume", 0),
            ]
        elif isinstance(row, (list, tuple)) and len(row) >= 6:
            values = list(row[:6])
        else:
            continue
        if values[0] is None:
            continue
        output.append(values)
    output.sort(key=lambda item: str(item[0]))
    return output


def _resolve_zerodha_index_token(broker, instrument):
    if instrument in _ZERODHA_INDEX_TOKEN_CACHE:
        return _ZERODHA_INDEX_TOKEN_CACHE[instrument]

    exchange, accepted_names = ZERODHA_INDEX_NAMES[instrument]
    for row in broker.kite.instruments(exchange) or []:
        trading_symbol = str(row.get("tradingsymbol") or "").upper().strip()
        name = str(row.get("name") or "").upper().strip()
        instrument_type = str(row.get("instrument_type") or "").upper().strip()
        if trading_symbol not in accepted_names and name not in accepted_names:
            continue
        if instrument_type and instrument_type not in {"INDICES", "INDEX"}:
            continue
        token = str(row.get("instrument_token") or "").strip()
        if token:
            _ZERODHA_INDEX_TOKEN_CACHE[instrument] = (token, exchange)
            return token, exchange
    raise RuntimeError(f"{instrument} index token not found")


def _request_arguments(broker_name, broker, instrument, days):
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    from_day = (now_ist - timedelta(days=max(int(days) - 1, 0))).replace(
        hour=9, minute=15, second=0, microsecond=0
    )
    interval = "1m" if int(days) == 1 else "15m" if int(days) == 7 else "1h"

    if broker_name == "angelone":
        return (
            INDEX_TOKENS[instrument],
            interval,
            from_day.strftime("%Y-%m-%d %H:%M"),
            now_ist.strftime("%Y-%m-%d %H:%M"),
            INDEX_EXCHANGE[instrument],
        )
    if broker_name == "upstox":
        return (
            UPSTOX_INDEX_KEYS[instrument],
            interval,
            from_day.strftime("%Y-%m-%d"),
            now_ist.strftime("%Y-%m-%d"),
            "BSE_INDEX" if instrument == "SENSEX" else "NSE_INDEX",
        )
    if broker_name == "zerodha":
        token, exchange = _resolve_zerodha_index_token(broker, instrument)
        return (
            token,
            interval,
            from_day.strftime("%Y-%m-%d %H:%M:%S"),
            now_ist.strftime("%Y-%m-%d %H:%M:%S"),
            exchange,
        )
    raise RuntimeError(f"Unsupported broker: {broker_name}")


def _is_rate_limit_error(message):
    value = str(message or "").lower()
    return any(
        phrase in value
        for phrase in (
            "exceeding access rate",
            "too many request",
            "rate limit",
            "couldn't parse the json response",
            "access denied",
            "status code 429",
            "ab1004",
        )
    )


def _friendly_error(broker_name, message):
    if broker_name == "angelone" and _is_rate_limit_error(message):
        return (
            "Angel One historical API rate limit active hai. "
            "45 seconds baad pull-down refresh karein."
        )
    if "token" in str(message or "").lower() or "session" in str(message or "").lower():
        return f"{broker_name.title()} token/session refresh karke retry karein."
    return f"{broker_name.title()} se historical candles nahi mili. Broker Test karke retry karein."


def _fetch_from_broker(row, instrument, days):
    broker_name = str(row["broker_name"] or "").lower().strip()
    broker = create_broker(
        broker_name,
        row["client_id"],
        decrypt_credential(row["api_key"]),
        decrypt_credential(row["api_secret"]),
        decrypt_credential(row["totp_secret"]) if row["totp_secret"] else None,
    )
    login_result = broker.login()
    if not isinstance(login_result, dict) or not login_result.get("success"):
        raise RuntimeError(str((login_result or {}).get("message") or "Broker login failed"))

    args = _request_arguments(broker_name, broker, instrument, days)
    retry_delays = (0, 2.0, 5.0) if broker_name == "angelone" else (0,)
    last_message = "Historical candle request failed"
    for delay in retry_delays:
        if delay:
            time.sleep(delay)
        _throttle(broker_name)
        result = broker.get_candles(*args[:4], exchange=args[4])
        if isinstance(result, dict) and result.get("success"):
            rows = _normalise_rows(result.get("candles") or [])
            if rows:
                return rows, broker_name
            last_message = "Broker returned no usable historical candles"
        else:
            last_message = str((result or {}).get("message") or last_message)
        if not _is_rate_limit_error(last_message):
            break
    raise RuntimeError(last_message)


def get_historical_rows(user_id, instrument, days):
    instrument = str(instrument or "NIFTY").upper()
    days = int(days or 1)
    key = (int(user_id), instrument, days)
    now = time.time()

    cached = _CACHE.get(key)
    if cached and now - cached["saved_at"] <= _cache_ttl(days):
        return cached["rows"], None, cached["broker"], True

    error_cached = _ERROR_CACHE.get(key)
    if error_cached and now - error_cached["saved_at"] <= 45:
        if cached and cached.get("rows"):
            return cached["rows"], error_cached["message"], cached["broker"], True
        return [], error_cached["message"], error_cached.get("broker"), True

    with _lock_for(key):
        now = time.time()
        cached = _CACHE.get(key)
        if cached and now - cached["saved_at"] <= _cache_ttl(days):
            return cached["rows"], None, cached["broker"], True

        conn = get_db()
        try:
            brokers = conn.execute(
                """
                SELECT * FROM broker_credentials
                WHERE user_id=? AND is_active=1
                ORDER BY datetime(last_connected) DESC, id DESC
                """,
                (int(user_id),),
            ).fetchall()
        finally:
            conn.close()

        if not brokers:
            message = "Historical graph ke liye broker connect aur test karein."
            _ERROR_CACHE[key] = {"message": message, "broker": None, "saved_at": now}
            return [], message, None, False

        errors = []
        for row in brokers:
            broker_name = str(row["broker_name"] or "").lower().strip()
            if broker_name not in {"angelone", "upstox", "zerodha"}:
                continue
            try:
                rows, used_broker = _fetch_from_broker(row, instrument, days)
                _CACHE[key] = {
                    "rows": rows,
                    "broker": used_broker,
                    "saved_at": time.time(),
                }
                _ERROR_CACHE.pop(key, None)
                return rows, None, used_broker, False
            except Exception as exc:
                errors.append((broker_name, str(exc)))

        broker_name, raw_message = errors[0] if errors else ("broker", "No supported broker")
        message = _friendly_error(broker_name, raw_message)
        _ERROR_CACHE[key] = {
            "message": message,
            "broker": broker_name,
            "saved_at": time.time(),
        }
        if cached and cached.get("rows"):
            return cached["rows"], message, cached["broker"], True
        return [], message, broker_name, False
