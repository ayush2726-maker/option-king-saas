from pathlib import Path

path = Path("bot/routes.py")
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global text
    if old not in text:
        raise RuntimeError(f"{label} marker not found")
    text = text.replace(old, new, 1)


# Store instrument on history rows and retain 35 days instead of only 200 points.
replace_once(
    '''        CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            price REAL,
            score INTEGER,
            signal TEXT,
            adx REAL,
            volume_ratio REAL,
            engine_updated_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
''',
    '''        CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            instrument TEXT,
            price REAL,
            score INTEGER,
            signal TEXT,
            adx REAL,
            volume_ratio REAL,
            engine_updated_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    try:
        conn.execute("ALTER TABLE signal_history ADD COLUMN instrument TEXT")
    except Exception:
        pass

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_signal_history_user_date
        ON signal_history(user_id, created_at DESC)
        """
    )
''',
    "signal history schema",
)

replace_once(
    '''            """INSERT INTO signal_history
               (user_id, price, score, signal, adx, volume_ratio, engine_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                float(engine_state.get("price", 0) or 0),
''',
    '''            """INSERT INTO signal_history
               (user_id, instrument, price, score, signal, adx, volume_ratio, engine_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                str(engine_state.get("underlying") or engine_state.get("chart_instrument") or "NIFTY").upper(),
                float(engine_state.get("price", 0) or 0),
''',
    "signal history insert",
)

replace_once(
    '''        conn.execute(
            """DELETE FROM signal_history WHERE user_id=? AND id NOT IN (
                   SELECT id FROM signal_history WHERE user_id=? ORDER BY id DESC LIMIT 200
               )""",
            (user_id, user_id)
        )
''',
    '''        conn.execute(
            """
            DELETE FROM signal_history
            WHERE user_id=?
              AND datetime(created_at) < datetime('now', '-35 days')
            """,
            (user_id,),
        )
        conn.execute(
            """DELETE FROM signal_history WHERE user_id=? AND id NOT IN (
                   SELECT id FROM signal_history WHERE user_id=? ORDER BY id DESC LIMIT 20000
               )""",
            (user_id, user_id)
        )
''',
    "signal history retention",
)

# Add historical candle helpers before chart endpoint.
marker = '''@router.get("/chart-data")
def get_chart_data(
'''
helpers = '''def _chart_range_days(value):
    try:
        days = int(value or 1)
    except Exception:
        days = 1
    return days if days in (1, 7, 30) else 1


def _historical_indicator_candles(rows):
    if not rows:
        return []

    import pandas as pd

    df = pd.DataFrame(
        rows,
        columns=["time", "open", "high", "low", "close", "volume"],
    )
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    if df.empty:
        return []

    parsed = pd.to_datetime(df["time"], errors="coerce")
    df = df.loc[parsed.notna()].copy()
    parsed = parsed.loc[parsed.notna()]
    if df.empty:
        return []

    try:
        if parsed.dt.tz is not None:
            local_time = parsed.dt.tz_convert("Asia/Kolkata")
        else:
            local_time = parsed
    except Exception:
        local_time = pd.to_datetime(df["time"], errors="coerce", utc=True).dt.tz_convert("Asia/Kolkata")

    df["_local_time"] = local_time
    df = df[
        (df["_local_time"].dt.weekday < 5)
        & ((df["_local_time"].dt.hour * 60 + df["_local_time"].dt.minute) >= 555)
        & ((df["_local_time"].dt.hour * 60 + df["_local_time"].dt.minute) <= 930)
    ].copy()
    if df.empty:
        return []

    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].fillna(0).clip(lower=0)
    day_key = df["_local_time"].dt.strftime("%Y-%m-%d")
    cumulative_pv = (typical * volume).groupby(day_key).cumsum()
    cumulative_volume = volume.groupby(day_key).cumsum().replace(0, float("nan"))
    df["vwap"] = (cumulative_pv / cumulative_volume).fillna(df["close"])

    output = []
    for _, row in df.tail(1800).iterrows():
        output.append({
            "time": str(row["time"]),
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
            "volume": float(row["volume"] or 0),
            "ema9": round(float(row["ema9"]), 2),
            "ema21": round(float(row["ema21"]), 2),
            "vwap": round(float(row["vwap"]), 2),
            "supertrend": None,
        })
    return output


def _fetch_angel_historical_candles(user_id, instrument, days):
    instrument = str(instrument or "NIFTY").upper()
    if instrument not in INDEX_TOKENS:
        return [], "UNSUPPORTED_INSTRUMENT", None

    conn = get_db()
    try:
        broker = conn.execute(
            """
            SELECT * FROM broker_credentials
            WHERE user_id=? AND is_active=1
            ORDER BY last_connected DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    if not broker:
        return [], "BROKER_NOT_CONNECTED", None
    if str(broker["broker_name"] or "").lower() != "angelone":
        return [], "HISTORICAL_RANGE_CURRENTLY_REQUIRES_ANGELONE", None

    creds = {
        "api_key": decrypt_credential(broker["api_key"]),
        "client_id": broker["client_id"],
        "password": decrypt_credential(broker["api_secret"]),
        "totp_secret": decrypt_credential(broker["totp_secret"]) if broker["totp_secret"] else None,
    }
    obj = angel_login(creds)

    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    from_day = (now_ist - timedelta(days=max(days - 1, 0))).replace(
        hour=9, minute=15, second=0, microsecond=0
    )
    interval = "ONE_MINUTE" if days == 1 else "FIFTEEN_MINUTE" if days == 7 else "ONE_HOUR"
    params = {
        "exchange": INDEX_EXCHANGE[instrument],
        "symboltoken": INDEX_TOKENS[instrument],
        "interval": interval,
        "fromdate": from_day.strftime("%Y-%m-%d %H:%M"),
        "todate": now_ist.strftime("%Y-%m-%d %H:%M"),
    }
    response = obj.getCandleData(params)
    if not isinstance(response, dict) or response.get("status") is False:
        return [], "BROKER_HISTORY_ERROR", interval
    rows = response.get("data") or []
    return _historical_indicator_candles(rows), None, interval


'''
if marker not in text:
    raise RuntimeError("chart endpoint marker not found")
text = text.replace(marker, helpers + marker, 1)

# Replace chart endpoint with range-aware implementation.
start = text.index('@router.get("/chart-data")')
end = text.index('\n\n@router.get("/signal-history")', start)
old_chart = text[start:end]
new_chart = '''@router.get("/chart-data")
def get_chart_data(
    authorization: str = Header(None),
    instrument: str = "NIFTY",
    days: int = 1,
):
    user = get_current_user(authorization)
    requested_instrument = str(instrument or "NIFTY").upper()
    requested_days = _chart_range_days(days)

    state = get_user_bot_state(user["id"])
    auto_restarted = False
    recovery_reason = None

    state_instrument = str(
        state.get("chart_instrument") or state.get("underlying") or "NIFTY"
    ).upper()
    live_candles = state.get("chart_candles", []) or []

    if requested_days == 1 and requested_instrument == state_instrument and live_candles:
        candles = live_candles
        interval = state.get("chart_interval", "ONE_MINUTE")
        source = "LIVE_ENGINE"
        history_reason = None
    else:
        candles = []
        interval = None
        source = "ANGELONE_HISTORICAL"
        history_reason = None
        try:
            candles, history_reason, interval = _fetch_angel_historical_candles(
                user["id"], requested_instrument, requested_days
            )
        except Exception as exc:
            history_reason = "HISTORY_FETCH_FAILED: " + str(exc)[:140]

        if not candles and requested_days == 1:
            if not state.get("running"):
                recovery = _start_saved_runtime_engine(user["id"])
                state = recovery["state"]
                auto_restarted = bool(recovery["started"])
                recovery_reason = recovery["reason"]
            state_instrument = str(
                state.get("chart_instrument") or state.get("underlying") or "NIFTY"
            ).upper()
            if requested_instrument == state_instrument:
                candles = state.get("chart_candles", []) or []
                interval = state.get("chart_interval", "ONE_MINUTE")
                source = "LIVE_ENGINE_FALLBACK"

    raw_status = state.get("status", "NOT_STARTED")
    if candles:
        chart_status = "HISTORY_READY" if requested_days > 1 else raw_status
        message = f"{len(candles)} candles loaded • {requested_days} day range"
    elif auto_restarted:
        chart_status = "AUTO_RESTARTING"
        message = "Broker engine auto-restarted. Candles load ho rahi hain."
    elif history_reason:
        chart_status = "HISTORY_UNAVAILABLE"
        message = history_reason
    elif recovery_reason == "BOT_STOPPED":
        chart_status = "BOT_STOPPED"
        message = "Bot stopped hai."
    elif recovery_reason == "BROKER_NOT_CONNECTED":
        chart_status = "BROKER_NOT_CONNECTED"
        message = "Active broker credentials nahi mile."
    else:
        chart_status = raw_status
        message = "Broker engine candles prepare kar raha hai."

    return {
        "success": True,
        "running": bool(state.get("running")),
        "runtime_running": bool(state.get("running")),
        "auto_restarted": auto_restarted,
        "status": chart_status,
        "runtime_status": raw_status,
        "reason": history_reason or recovery_reason,
        "message": message,
        "instrument": requested_instrument,
        "interval": interval or "ONE_MINUTE",
        "range_days": requested_days,
        "source": source,
        "count": len(candles),
        "candles": candles,
        "updated_at": state.get("updated_at") or datetime.utcnow().isoformat(),
    }
'''
text = text[:start] + new_chart + text[end:]

# Replace signal-history endpoint with date/instrument aware implementation.
start = text.index('@router.get("/signal-history")')
old_signal = text[start:]
# Endpoint is last function in this file, so replace to EOF.
new_signal = '''@router.get("/signal-history")
def get_signal_history(
    authorization: str = Header(None),
    limit: int = 1000,
    days: int = 1,
    instrument: str = "",
):
    user = get_current_user(authorization)
    requested_days = _chart_range_days(days)
    requested_instrument = str(instrument or "").upper().strip()
    safe_limit = max(50, min(int(limit or 1000), 10000))

    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    start_ist = (now_ist - timedelta(days=max(requested_days - 1, 0))).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = start_ist - timedelta(hours=5, minutes=30)

    conn = get_db()
    ensure_tables(conn)
    query = """
        SELECT instrument, price, score, signal, adx, volume_ratio, created_at
        FROM signal_history
        WHERE user_id=? AND datetime(created_at) >= datetime(?)
    """
    params = [user["id"], start_utc.strftime("%Y-%m-%d %H:%M:%S")]
    if requested_instrument:
        query += " AND (instrument=? OR instrument IS NULL OR instrument='')"
        params.append(requested_instrument)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(safe_limit)

    rows = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    points = [
        {
            "instrument": r["instrument"] or requested_instrument or None,
            "price": r["price"],
            "score": r["score"],
            "signal": r["signal"],
            "adx": r["adx"],
            "volume_ratio": r["volume_ratio"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    points.reverse()
    return {
        "success": True,
        "count": len(points),
        "range_days": requested_days,
        "instrument": requested_instrument or None,
        "points": points,
    }
'''
text = text[:start] + new_signal + "\n"

path.write_text(text, encoding="utf-8")
print("Historical chart backend patch applied")
