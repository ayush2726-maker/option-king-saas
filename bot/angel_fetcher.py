"""
Option King AI SaaS - Angel One Data Fetcher
Same logic as personal bot app.py
"""
import time
import threading
from datetime import datetime, timezone, timedelta
from SmartApi import SmartConnect
import pyotp
from bot.strategy import get_full_signal, is_hero_window_active

# ── Per-user bot instances ────────────────────────────────
_user_bots = {}  # user_id -> bot state
_lock = threading.Lock()

NIFTY_TOKEN  = "26000"
NIFTY_SYMBOL = "Nifty 50"
BANK_TOKEN   = "26009"
BANK_SYMBOL  = "Nifty Bank"

# ── Angel One Login (same as app.py) ────────────────────
def angel_login(creds: dict):
    """
    creds = {api_key, client_id, password, totp_secret}
    Returns SmartConnect obj or raises
    """
    required = ["api_key", "client_id", "password", "totp_secret"]
    missing = [k for k in required if not creds.get(k)]
    if missing:
        raise RuntimeError(f"Missing credentials: {missing}")

    for attempt in range(3):
        try:
            obj = SmartConnect(api_key=creds["api_key"])
            totp = pyotp.TOTP(creds["totp_secret"]).now()
            session = obj.generateSession(
                creds["client_id"],
                creds["password"],
                totp
            )
            if not session or session.get("status") is False:
                raise RuntimeError(f"Login failed: {session}")
            return obj
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"Login failed after retries: {e}")
            time.sleep(3)

# ── Get Candles (same as app.py get_candles) ─────────────
def get_candles(obj, token: str, interval: str = "ONE_MINUTE"):
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    from_dt = now_ist.replace(hour=9, minute=15,
                               second=0, microsecond=0)
    params = {
        "exchange": "NSE",
        "symboltoken": token,
        "interval": interval,
        "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate": now_ist.strftime("%Y-%m-%d %H:%M"),
    }
    data = obj.getCandleData(params)
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid candle response: {str(data)[:120]}")
    if data.get("status") is False:
        raise RuntimeError(str(data)[:240])
    rows = data.get("data", [])
    if not rows:
        return None

    import pandas as pd
    df = pd.DataFrame(rows, columns=["time","open","high","low","close","volume"])
    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna()
    return df if not df.empty else None

# ── Calculate Indicators ──────────────────────────────────
def calculate_indicators(df):
    """EMA9, EMA21, VWAP, ATR, ADX, Volume ratio"""
    if df is None or len(df) < 21:
        return None

    # EMA
    df["EMA9"]  = df["close"].ewm(span=9,  adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()

    # VWAP
    df["TP"] = (df["high"] + df["low"] + df["close"]) / 3
    df["VWAP"] = (df["TP"] * df["volume"]).cumsum() / df["volume"].cumsum()

    # ATR
    df["H-L"]  = df["high"] - df["low"]
    df["H-PC"] = (df["high"] - df["close"].shift(1)).abs()
    df["L-PC"] = (df["low"]  - df["close"].shift(1)).abs()
    df["TR"]   = df[["H-L","H-PC","L-PC"]].max(axis=1)
    df["ATR"]  = df["TR"].ewm(span=14, adjust=False).mean()

    # ADX
    df["DM+"] = (df["high"] - df["high"].shift(1)).clip(lower=0)
    df["DM-"] = (df["low"].shift(1) - df["low"]).clip(lower=0)
    df["DI+"] = 100 * df["DM+"].ewm(span=14, adjust=False).mean() / df["ATR"]
    df["DI-"] = 100 * df["DM-"].ewm(span=14, adjust=False).mean() / df["ATR"]
    dx = (df["DI+"] - df["DI-"]).abs() / (df["DI+"] + df["DI-"] + 1e-9) * 100
    df["ADX"] = dx.ewm(span=14, adjust=False).mean()

    # Volume ratio
    df["VOL_MA"] = df["volume"].rolling(20).mean()
    df["VOL_RATIO"] = df["volume"] / df["VOL_MA"].replace(0, 1)

    # Supertrend (simplified)
    df["UPPER"] = df["TP"] + (2.0 * df["ATR"])
    df["LOWER"] = df["TP"] - (2.0 * df["ATR"])
    df["ST_DIR"] = "NEUTRAL"
    for i in range(1, len(df)):
        if df["close"].iloc[i] > df["UPPER"].iloc[i-1]:
            df.loc[df.index[i], "ST_DIR"] = "UP"
        elif df["close"].iloc[i] < df["LOWER"].iloc[i-1]:
            df.loc[df.index[i], "ST_DIR"] = "DOWN"
        else:
            df.loc[df.index[i], "ST_DIR"] = df["ST_DIR"].iloc[i-1]

    # Trend
    last = df.iloc[-1]
    if last["EMA9"] > last["EMA21"]:
        trend = "UPTREND"
    elif last["EMA9"] < last["EMA21"]:
        trend = "DOWNTREND"
    else:
        trend = "SIDEWAYS"

    return df, trend

# ── Per-user Bot Loop ────────────────────────────────────
def run_user_bot(user_id: int, creds: dict, state: dict):
    """Runs in background thread for each user"""
    obj = None
    while state.get("running"):
        try:
            # Login if needed
            if obj is None:
                obj = angel_login(creds)
                state["status"] = "LOGGED_IN"

            # Get candles
            df = get_candles(obj, NIFTY_TOKEN)
            if df is None or len(df) < 28:
                state["status"] = "WAITING_CANDLES"
                time.sleep(30)
                continue

            result = calculate_indicators(df)
            if result is None:
                time.sleep(30)
                continue

            df, trend = result
            last = df.iloc[-2]  # Use closed candle
            c1   = df.iloc[-3]
            c2   = df.iloc[-2]

            market_data = {
                "price":         float(last["close"]),
                "vwap":          float(last["VWAP"]),
                "ema9":          float(last["EMA9"]),
                "ema21":         float(last["EMA21"]),
                "adx":           float(last["ADX"]),
                "volume_ratio":  float(last["VOL_RATIO"]),
                "supertrend_dir":str(last["ST_DIR"]),
                "trend":         trend,
                "mtf_confirmed": trend != "SIDEWAYS",
                "c1_bullish":    float(c1["close"]) > float(c1["open"]),
                "c2_bullish":    float(c2["close"]) > float(c2["open"]),
                "gap_day":       False,
                "orb_high":      float(df[df["time"] <= df["time"].iloc[0] + "09:30"]["high"].max()) if len(df) > 5 else 0,
                "orb_low":       0,
                "atr":           float(last["ATR"]),
            }

            # Get signal
            signal_data = get_full_signal(market_data)

            # Hero window check
            hero = is_hero_window_active()

            # Update state
            state.update({
                **signal_data,
                "hero": hero,
                "price": market_data["price"],
                "status": "RUNNING",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        except Exception as e:
            obj = None  # Force re-login
            state["status"] = f"ERROR: {str(e)[:100]}"
            time.sleep(60)

        time.sleep(60)  # Check every minute

# ── Start/Stop Bot for User ───────────────────────────────
def start_user_bot(user_id: int, creds: dict) -> dict:
    with _lock:
        if user_id in _user_bots and _user_bots[user_id].get("running"):
            return {"success": False, "message": "Bot already running"}

        state = {
            "running": True,
            "status": "STARTING",
            "signal": "WAITING",
            "score": 0,
            "user_id": user_id,
        }
        _user_bots[user_id] = state

        t = threading.Thread(
            target=run_user_bot,
            args=(user_id, creds, state),
            daemon=True
        )
        t.start()
        return {"success": True, "message": "Bot started"}

def stop_user_bot(user_id: int) -> dict:
    with _lock:
        if user_id not in _user_bots:
            return {"success": False, "message": "Bot not running"}
        _user_bots[user_id]["running"] = False
        del _user_bots[user_id]
        return {"success": True, "message": "Bot stopped"}

def get_user_bot_state(user_id: int) -> dict:
    return _user_bots.get(user_id, {
        "running": False,
        "status": "NOT_STARTED",
        "signal": "WAITING",
        "score": 0,
    })
