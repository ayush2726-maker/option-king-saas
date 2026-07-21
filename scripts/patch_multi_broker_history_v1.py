from pathlib import Path

path = Path("bot/routes.py")
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global text
    if old not in text:
        raise RuntimeError(f"{label} marker not found")
    text = text.replace(old, new, 1)


replace_once(
    "from bot.strategy import is_hero_window_active\n",
    "from bot.strategy import is_hero_window_active\nfrom bot.brokers.factory import create_broker\n",
    "broker factory import",
)

start = text.index("def _fetch_angel_historical_candles(")
end = text.index("\n\n@router.get(\"/chart-data\")", start)

new_helpers = '''UPSTOX_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}

ZERODHA_INDEX_NAMES = {
    "NIFTY": ("NSE", {"NIFTY 50", "NIFTY"}),
    "BANKNIFTY": ("NSE", {"NIFTY BANK", "BANKNIFTY"}),
    "SENSEX": ("BSE", {"SENSEX"}),
}

_ZERODHA_INDEX_TOKEN_CACHE = {}


def _normalise_broker_candle_rows(rows):
    normalised = []
    for row in rows or []:
        if isinstance(row, dict):
            timestamp = row.get("date") or row.get("time") or row.get("timestamp")
            values = [
                timestamp,
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
        normalised.append(values)
    normalised.sort(key=lambda item: str(item[0]))
    return normalised


def _resolve_zerodha_index_token(broker, instrument):
    instrument = str(instrument or "NIFTY").upper()
    if instrument in _ZERODHA_INDEX_TOKEN_CACHE:
        return _ZERODHA_INDEX_TOKEN_CACHE[instrument]

    exchange, accepted_names = ZERODHA_INDEX_NAMES[instrument]
    rows = broker.kite.instruments(exchange)
    for row in rows or []:
        trading_symbol = str(row.get("tradingsymbol") or "").upper().strip()
        name = str(row.get("name") or "").upper().strip()
        instrument_type = str(row.get("instrument_type") or "").upper().strip()
        if trading_symbol in accepted_names or name in accepted_names:
            if instrument_type and instrument_type not in {"INDICES", "INDEX"}:
                continue
            token = str(row.get("instrument_token") or "").strip()
            if token:
                _ZERODHA_INDEX_TOKEN_CACHE[instrument] = (token, exchange)
                return token, exchange
    raise RuntimeError(f"{instrument} index token not found in Zerodha instruments")


def _broker_history_request(broker_name, broker, instrument, days):
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    from_day = (now_ist - timedelta(days=max(days - 1, 0))).replace(
        hour=9, minute=15, second=0, microsecond=0
    )
    interval = "1m" if days == 1 else "15m" if days == 7 else "1h"

    if broker_name == "angelone":
        symbol = INDEX_TOKENS[instrument]
        exchange = INDEX_EXCHANGE[instrument]
        from_value = from_day.strftime("%Y-%m-%d %H:%M")
        to_value = now_ist.strftime("%Y-%m-%d %H:%M")
    elif broker_name == "upstox":
        symbol = UPSTOX_INDEX_KEYS[instrument]
        exchange = "BSE_INDEX" if instrument == "SENSEX" else "NSE_INDEX"
        from_value = from_day.strftime("%Y-%m-%d")
        to_value = now_ist.strftime("%Y-%m-%d")
    elif broker_name == "zerodha":
        symbol, exchange = _resolve_zerodha_index_token(broker, instrument)
        from_value = from_day.strftime("%Y-%m-%d %H:%M:%S")
        to_value = now_ist.strftime("%Y-%m-%d %H:%M:%S")
    else:
        raise RuntimeError(f"Historical candles are not supported for {broker_name}")

    response = broker.get_candles(
        symbol,
        interval,
        from_value,
        to_value,
        exchange=exchange,
    )
    if not isinstance(response, dict) or not response.get("success"):
        raise RuntimeError(str((response or {}).get("message") or "Broker candle request failed"))

    rows = _normalise_broker_candle_rows(response.get("candles") or [])
    candles = _historical_indicator_candles(rows)
    if not candles:
        raise RuntimeError("Broker returned no usable historical candles")
    display_interval = "ONE_MINUTE" if days == 1 else "FIFTEEN_MINUTE" if days == 7 else "ONE_HOUR"
    return candles, display_interval


def _fetch_broker_historical_candles(user_id, instrument, days):
    instrument = str(instrument or "NIFTY").upper()
    if instrument not in INDEX_TOKENS:
        return [], "Unsupported chart instrument", None, None

    conn = get_db()
    try:
        brokers = conn.execute(
            """
            SELECT * FROM broker_credentials
            WHERE user_id=? AND is_active=1
            ORDER BY datetime(last_connected) DESC, id DESC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    if not brokers:
        return [], "Connect and test a broker to load historical candles", None, None

    errors = []
    for row in brokers:
        broker_name = str(row["broker_name"] or "").lower().strip()
        if broker_name not in {"angelone", "upstox", "zerodha"}:
            continue
        try:
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
            candles, interval = _broker_history_request(
                broker_name,
                broker,
                instrument,
                days,
            )
            return candles, None, interval, broker_name
        except Exception as exc:
            errors.append(f"{broker_name}: {str(exc)[:110]}")

    detail = "; ".join(errors[:3])
    message = "Historical data load nahi hua. Broker token/credentials refresh karke retry karo."
    if detail:
        message += " " + detail
    return [], message, None, None
'''

text = text[:start] + new_helpers + text[end:]

replace_once(
    '''    state = get_user_bot_state(user["id"])
    auto_restarted = False
    recovery_reason = None
''',
    '''    state = get_user_bot_state(user["id"])
    auto_restarted = False
    recovery_reason = None
    history_broker = None
''',
    "history broker init",
)

replace_once(
    '''        source = "ANGELONE_HISTORICAL"
        history_reason = None
        try:
            candles, history_reason, interval = _fetch_angel_historical_candles(
                user["id"], requested_instrument, requested_days
            )
''',
    '''        source = "BROKER_HISTORICAL"
        history_reason = None
        try:
            candles, history_reason, interval, history_broker = _fetch_broker_historical_candles(
                user["id"], requested_instrument, requested_days
            )
            if history_broker:
                source = f"{history_broker.upper()}_HISTORICAL"
''',
    "generic history fetch",
)

replace_once(
    '''        "source": source,
        "count": len(candles),
''',
    '''        "source": source,
        "broker": history_broker,
        "count": len(candles),
''',
    "history broker response",
)

path.write_text(text, encoding="utf-8")
print("Multi-broker historical chart patch applied")
