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
    "from bot.strategy import is_hero_window_active, get_full_signal\n"
    "from strategy.profile_engine import get_active_profile_config\n",
    "strategy imports",
)

# Replace the snapshot logger so AUTO portfolio saves a score row for every
# scanned index, not only the selected/best index.
start = text.index("def log_signal_snapshot(")
end = text.index("\ndef get_strategy_settings", start)
new_logger = '''def _snapshot_number(value, default=0.0):
    try:
        number = float(value)
        if number != number:
            return default
        return number
    except Exception:
        return default


def log_signal_snapshot(conn, user_id: int, engine_state: dict):
    """Save one score point per instrument and completed engine update."""
    try:
        engine_updated_at = engine_state.get("updated_at")
        if not engine_updated_at:
            return

        scan_results = engine_state.get("scan_results")
        snapshots = []

        if isinstance(scan_results, list) and scan_results:
            for scan in scan_results:
                if not isinstance(scan, dict):
                    continue
                instrument = str(
                    scan.get("underlying")
                    or scan.get("instrument")
                    or scan.get("symbol")
                    or ""
                ).upper().strip()
                if not instrument:
                    continue
                snapshots.append({
                    "instrument": instrument,
                    "price": _snapshot_number(
                        scan.get("price")
                        or scan.get("spot_price")
                        or scan.get("ltp")
                    ),
                    "score": int(_snapshot_number(scan.get("score"), 0)),
                    "signal": str(
                        scan.get("candidate_signal")
                        or scan.get("signal")
                        or "WAIT"
                    ),
                    "adx": _snapshot_number(scan.get("adx"), 0),
                    "volume_ratio": _snapshot_number(
                        scan.get("volume_ratio"),
                        0,
                    ),
                    "updated_at": str(
                        scan.get("updated_at")
                        or engine_updated_at
                    ),
                })

        if not snapshots:
            snapshots.append({
                "instrument": str(
                    engine_state.get("underlying")
                    or engine_state.get("chart_instrument")
                    or "NIFTY"
                ).upper(),
                "price": _snapshot_number(engine_state.get("price"), 0),
                "score": int(_snapshot_number(engine_state.get("score"), 0)),
                "signal": str(engine_state.get("signal") or "WAIT"),
                "adx": _snapshot_number(engine_state.get("adx"), 0),
                "volume_ratio": _snapshot_number(
                    engine_state.get("volume_ratio"),
                    0,
                ),
                "updated_at": str(engine_updated_at),
            })

        for snapshot in snapshots:
            exists = conn.execute(
                """
                SELECT 1 FROM signal_history
                WHERE user_id=? AND instrument=? AND engine_updated_at=?
                LIMIT 1
                """,
                (
                    user_id,
                    snapshot["instrument"],
                    snapshot["updated_at"],
                ),
            ).fetchone()
            if exists:
                continue

            conn.execute(
                """
                INSERT INTO signal_history (
                    user_id, instrument, price, score, signal,
                    adx, volume_ratio, engine_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    snapshot["instrument"],
                    snapshot["price"],
                    snapshot["score"],
                    snapshot["signal"],
                    snapshot["adx"],
                    snapshot["volume_ratio"],
                    snapshot["updated_at"],
                ),
            )

        conn.execute(
            """
            DELETE FROM signal_history
            WHERE user_id=?
              AND datetime(created_at) < datetime('now', '-35 days')
            """,
            (user_id,),
        )
        conn.execute(
            """
            DELETE FROM signal_history
            WHERE user_id=? AND id NOT IN (
                SELECT id FROM signal_history
                WHERE user_id=? ORDER BY id DESC LIMIT 20000
            )
            """,
            (user_id, user_id),
        )
        conn.commit()
    except Exception as exc:
        print(f"SIGNAL HISTORY WARNING | user={user_id} | {str(exc)[:160]}")

'''
text = text[:start] + new_logger + text[end + 1:]

# Replace historical indicator preparation with full strategy replay scoring.
start = text.index("def _historical_indicator_candles(")
end = text.index("\ndef _fetch_broker_historical_candles", start)
new_history = '''def _historical_indicator_candles(rows, profile=None):
    if not rows:
        return []

    import math
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
    valid = parsed.notna()
    df = df.loc[valid].copy()
    parsed = parsed.loc[valid]
    if df.empty:
        return []

    try:
        if parsed.dt.tz is not None:
            local_time = parsed.dt.tz_convert("Asia/Kolkata")
        else:
            local_time = parsed
    except Exception:
        local_time = pd.to_datetime(
            df["time"],
            errors="coerce",
            utc=True,
        ).dt.tz_convert("Asia/Kolkata")

    df["_local_time"] = local_time
    df = df[
        (df["_local_time"].dt.weekday < 5)
        & ((df["_local_time"].dt.hour * 60 + df["_local_time"].dt.minute) >= 555)
        & ((df["_local_time"].dt.hour * 60 + df["_local_time"].dt.minute) <= 930)
    ].copy()
    if df.empty:
        return []

    df = df.sort_values("_local_time").reset_index(drop=True)
    df["DAY_KEY"] = df["_local_time"].dt.strftime("%Y-%m-%d")
    df["MINUTE"] = df["_local_time"].dt.hour * 60 + df["_local_time"].dt.minute

    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["TP"] = (df["high"] + df["low"] + df["close"]) / 3.0

    safe_volume = df["volume"].fillna(0).clip(lower=0)
    cumulative_volume = safe_volume.groupby(df["DAY_KEY"]).cumsum()
    cumulative_value = (df["TP"] * safe_volume).groupby(df["DAY_KEY"]).cumsum()
    session_average = (
        df.groupby("DAY_KEY")["TP"]
        .expanding(min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )
    weighted_vwap = cumulative_value / cumulative_volume.where(cumulative_volume > 0)
    df["VWAP"] = weighted_vwap.where(cumulative_volume > 0, session_average)
    df["VWAP"] = df["VWAP"].ffill().fillna(session_average).fillna(df["close"])
    df["VWAP_FALLBACK_USED"] = cumulative_volume <= 0

    df["H-L"] = df["high"] - df["low"]
    df["H-PC"] = (df["high"] - df["close"].shift(1)).abs()
    df["L-PC"] = (df["low"] - df["close"].shift(1)).abs()
    df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
    df["ATR"] = df["TR"].ewm(span=14, adjust=False).mean()

    df["DM+"] = (df["high"] - df["high"].shift(1)).clip(lower=0)
    df["DM-"] = (df["low"].shift(1) - df["low"]).clip(lower=0)
    df["DI+"] = 100 * df["DM+"].ewm(span=14, adjust=False).mean() / df["ATR"].replace(0, float("nan"))
    df["DI-"] = 100 * df["DM-"].ewm(span=14, adjust=False).mean() / df["ATR"].replace(0, float("nan"))
    dx = (df["DI+"] - df["DI-"]).abs() / (df["DI+"] + df["DI-"] + 1e-9) * 100
    df["ADX"] = dx.ewm(span=14, adjust=False).mean().fillna(0)

    df["VOL_MA"] = safe_volume.rolling(20, min_periods=1).mean()
    df["VOL_RATIO"] = (
        safe_volume / df["VOL_MA"].where(df["VOL_MA"] > 0)
    ).replace([float("inf"), -float("inf")], float("nan")).fillna(0)

    df["UPPER"] = df["TP"] + (2.0 * df["ATR"])
    df["LOWER"] = df["TP"] - (2.0 * df["ATR"])
    directions = ["NEUTRAL"] * len(df)
    for index in range(1, len(df)):
        if df.loc[index, "DAY_KEY"] != df.loc[index - 1, "DAY_KEY"]:
            directions[index] = "NEUTRAL"
        elif df.loc[index, "close"] > df.loc[index - 1, "UPPER"]:
            directions[index] = "UP"
        elif df.loc[index, "close"] < df.loc[index - 1, "LOWER"]:
            directions[index] = "DOWN"
        else:
            directions[index] = directions[index - 1]
    df["ST_DIR"] = directions

    df["ORB_HIGH"] = 0.0
    df["ORB_LOW"] = 0.0
    for _, group in df.groupby("DAY_KEY"):
        orb = group[(group["MINUTE"] >= 555) & (group["MINUTE"] <= 570)]
        if orb.empty:
            continue
        orb_high = float(orb["high"].max())
        orb_low = float(orb["low"].min())
        completed_indexes = group[group["MINUTE"] >= 570].index
        df.loc[completed_indexes, "ORB_HIGH"] = orb_high
        df.loc[completed_indexes, "ORB_LOW"] = orb_low

    def safe_number(value, default=0.0):
        try:
            number = float(value)
            return number if math.isfinite(number) else default
        except Exception:
            return default

    output = []
    active_profile = profile if isinstance(profile, dict) else None

    for index, row in df.tail(1800).iterrows():
        ema9 = safe_number(row["EMA9"], safe_number(row["close"]))
        ema21 = safe_number(row["EMA21"], safe_number(row["close"]))
        trend = "UPTREND" if ema9 > ema21 else "DOWNTREND" if ema9 < ema21 else "SIDEWAYS"
        direction = str(row["ST_DIR"] or "NEUTRAL").upper()

        previous = df.iloc[index - 1] if index > 0 else row
        c1_bullish = safe_number(previous["close"]) > safe_number(previous["open"])
        c2_bullish = safe_number(row["close"]) > safe_number(row["open"])

        signal_data = None
        if index >= 20:
            market_data = {
                "price": safe_number(row["close"]),
                "vwap": safe_number(row["VWAP"], safe_number(row["close"])),
                "ema9": ema9,
                "ema21": ema21,
                "adx": safe_number(row["ADX"]),
                "volume_ratio": safe_number(row["VOL_RATIO"]),
                "vwap_fallback_used": bool(row["VWAP_FALLBACK_USED"]),
                "supertrend_dir": direction,
                "trend": trend,
                "mtf_confirmed": trend != "SIDEWAYS",
                "c1_bullish": c1_bullish,
                "c2_bullish": c2_bullish,
                "gap_day": False,
                "orb_high": safe_number(row["ORB_HIGH"]),
                "orb_low": safe_number(row["ORB_LOW"]),
                "atr": safe_number(row["ATR"]),
            }
            signal_data = get_full_signal(
                market_data,
                consecutive_losses=0,
                profile=active_profile,
            )

        if direction == "UP":
            supertrend = safe_number(row["LOWER"], None)
        elif direction == "DOWN":
            supertrend = safe_number(row["UPPER"], None)
        else:
            supertrend = None

        score = (
            int(signal_data.get("score", 0))
            if isinstance(signal_data, dict)
            else None
        )
        candidate = (
            str(
                signal_data.get("candidate_signal")
                or signal_data.get("signal")
                or "WAIT"
            )
            if isinstance(signal_data, dict)
            else None
        )

        output.append({
            "time": str(row["time"]),
            "open": round(safe_number(row["open"]), 2),
            "high": round(safe_number(row["high"]), 2),
            "low": round(safe_number(row["low"]), 2),
            "close": round(safe_number(row["close"]), 2),
            "volume": safe_number(row["volume"]),
            "ema9": round(ema9, 2),
            "ema21": round(ema21, 2),
            "vwap": round(safe_number(row["VWAP"], safe_number(row["close"])), 2),
            "supertrend": round(supertrend, 2) if supertrend is not None else None,
            "supertrend_dir": direction,
            "adx": round(safe_number(row["ADX"]), 2),
            "atr": round(safe_number(row["ATR"]), 2),
            "volume_ratio": round(safe_number(row["VOL_RATIO"]), 2),
            "score": score,
            "signal": candidate,
            "trade_allowed": bool(signal_data.get("trade_allowed")) if isinstance(signal_data, dict) else False,
            "min_score": int(signal_data.get("min_score", 82)) if isinstance(signal_data, dict) else 82,
            "score_source": "HISTORICAL_REPLAY" if score is not None else None,
            "strategy_profile_key": (
                active_profile.get("profile_key", "okai_default_82")
                if active_profile else "okai_default_82"
            ),
            "strategy_profile_name": (
                active_profile.get("profile_name", "OKAI Default 82")
                if active_profile else "OKAI Default 82"
            ),
        })
    return output

'''
text = text[:start] + new_history + text[end + 1:]

replace_once(
    '''    candles = _historical_indicator_candles(rows)
    if rows and not candles:
''',
    '''    try:
        active_profile = get_active_profile_config(user_id)
    except Exception:
        active_profile = None
    candles = _historical_indicator_candles(rows, profile=active_profile)
    if rows and not candles:
''',
    "active profile replay",
)

replace_once(
    '''        "count": len(candles),
        "candles": candles,
''',
    '''        "count": len(candles),
        "score_count": sum(
            1 for candle in candles
            if candle.get("score") is not None
        ),
        "score_source": (
            "HISTORICAL_REPLAY"
            if any(candle.get("score") is not None for candle in candles)
            else "LIVE_SIGNAL_HISTORY"
        ),
        "strategy_profile_name": next(
            (
                candle.get("strategy_profile_name")
                for candle in reversed(candles)
                if candle.get("strategy_profile_name")
            ),
            None,
        ),
        "candles": candles,
''',
    "chart score metadata",
)

path.write_text(text, encoding="utf-8")
print("Historical score replay patch applied")
