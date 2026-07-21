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
    "from bot.strategy import is_hero_window_active\nfrom bot.history_provider import get_historical_rows\n",
    "history provider import",
)

start = text.index("def _fetch_angel_historical_candles(")
end = text.index("\n\n@router.get(\"/chart-data\")", start)
new_helper = '''def _fetch_broker_historical_candles(user_id, instrument, days):
    rows, reason, broker_name, cached = get_historical_rows(
        user_id,
        instrument,
        days,
    )
    interval = "ONE_MINUTE" if int(days) == 1 else "FIFTEEN_MINUTE" if int(days) == 7 else "ONE_HOUR"
    candles = _historical_indicator_candles(rows)
    if rows and not candles:
        reason = "Broker candles mili, lekin chart format prepare nahi hua."
    return candles, reason, interval, broker_name, cached
'''
text = text[:start] + new_helper + text[end:]

replace_once(
    '''    state = get_user_bot_state(user["id"])
    auto_restarted = False
    recovery_reason = None
''',
    '''    state = get_user_bot_state(user["id"])
    auto_restarted = False
    recovery_reason = None
    history_broker = None
    history_cached = False
''',
    "history state",
)

replace_once(
    '''        source = "ANGELONE_HISTORICAL"
        history_reason = None
        try:
            candles, history_reason, interval = _fetch_angel_historical_candles(
                user["id"], requested_instrument, requested_days
            )
        except Exception as exc:
            history_reason = "HISTORY_FETCH_FAILED: " + str(exc)[:140]
''',
    '''        source = "BROKER_HISTORICAL"
        history_reason = None
        try:
            candles, history_reason, interval, history_broker, history_cached = _fetch_broker_historical_candles(
                user["id"], requested_instrument, requested_days
            )
            if history_broker:
                source = f"{history_broker.upper()}_HISTORICAL"
            if history_cached:
                source += "_CACHE"
        except Exception:
            history_reason = "Historical graph load nahi hua. 45 seconds baad pull-down refresh karein."
''',
    "generic history fetch",
)

replace_once(
    '''        "source": source,
        "count": len(candles),
''',
    '''        "source": source,
        "broker": history_broker,
        "cached": history_cached,
        "count": len(candles),
''',
    "history response metadata",
)

path.write_text(text, encoding="utf-8")
print("Cached multi-broker historical chart patch applied")
