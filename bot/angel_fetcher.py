"""
Option King AI SaaS - Angel One Data Fetcher
Same logic as personal bot app.py
"""
import time
import threading
import json
from datetime import datetime, timezone, timedelta
from SmartApi import SmartConnect
import pyotp
from bot.strategy import (
    get_full_signal,
    is_hero_window_active,
    calculate_option_atr_levels,
)
from bot.option_chain import resolve_option
from database import get_db

# ── Per-user bot instances ────────────────────────────────
_user_bots = {}  # user_id -> bot state
_lock = threading.Lock()

NIFTY_TOKEN  = "26000"
NIFTY_SYMBOL = "Nifty 50"
BANK_TOKEN   = "26009"
BANK_SYMBOL  = "Nifty Bank"

LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}
INDEX_TOKENS = {"NIFTY": "26000", "BANKNIFTY": "26009", "SENSEX": "99919000"}
INDEX_EXCHANGE = {"NIFTY": "NSE", "BANKNIFTY": "NSE", "SENSEX": "BSE"}


def _read_settings(user_id):
    defaults = {
        "trading_mode": "paper",
        "primary_instrument": "NIFTY",
        "sl_percent": 12,
        "target_percent": 24,
        "entry_threshold": 82,
    }
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT settings_json FROM strategy_settings WHERE user_id=?",
            (user_id,)
        ).fetchone()
        conn.close()
        if row:
            saved = json.loads(row["settings_json"])
            defaults.update(saved)
    except Exception:
        pass
    return defaults


def calculate_orb_levels(
    df,
    start_minute: int = 9 * 60 + 15,
    end_minute: int = 9 * 60 + 30,
):
    """
    Calculate completed 09:15-09:30 IST opening-range high and low.

    Returns (0.0, 0.0) until the ORB window is complete or when
    candle timestamps cannot be parsed.
    """
    try:
        import pandas as pd

        if df is None or df.empty or "time" not in df.columns:
            return 0.0, 0.0

        times = pd.to_datetime(df["time"], errors="coerce")

        try:
            if times.dt.tz is not None:
                local_times = times.dt.tz_convert("Asia/Kolkata")
            else:
                local_times = times
        except (AttributeError, TypeError, ValueError):
            # Handles mixed timezone-aware timestamp formats.
            times = pd.to_datetime(
                df["time"],
                errors="coerce",
                utc=True,
            )
            local_times = times.dt.tz_convert("Asia/Kolkata")

        valid = local_times.notna()
        if not valid.any():
            return 0.0, 0.0

        minutes = (
            local_times.dt.hour * 60
            + local_times.dt.minute
        )

        # Do not use a partially formed ORB.
        if int(minutes[valid].max()) < end_minute:
            return 0.0, 0.0

        orb_mask = (
            valid
            & minutes.ge(start_minute)
            & minutes.le(end_minute)
        )
        orb_df = df.loc[orb_mask]

        if orb_df.empty:
            return 0.0, 0.0

        orb_high = float(orb_df["high"].max())
        orb_low = float(orb_df["low"].min())

        if orb_high <= 0 or orb_low <= 0:
            return 0.0, 0.0

        return orb_high, orb_low

    except Exception:
        return 0.0, 0.0


def _get_consecutive_losses_today(user_id):
    """
    Return today's current consecutive losing-trade streak for this user.

    A profitable or break-even closed trade resets the streak.
    Open trades are ignored.
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    day_start_utc = now_ist.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).astimezone(timezone.utc)

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT pnl
            FROM paper_trades
            WHERE user_id=?
              AND status='CLOSED'
              AND datetime(created_at) >= datetime(?)
            ORDER BY id DESC
            """,
            (
                user_id,
                day_start_utc.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        ).fetchall()

        streak = 0

        for row in rows:
            pnl = float(row["pnl"] or 0)

            if pnl < 0:
                streak += 1
            else:
                break

        return streak

    except Exception:
        return 0

    finally:
        conn.close()


def _manage_paper_trade(
    user_id,
    underlying,
    price,
    side,
    score,
    trade_allowed,
    settings,
    obj,
    spot_atr=0.0,
):
    """
    Checks/manages the user's open paper trade using REAL option premiums
    fetched from the broker. Closes on real SL/target hit, opens a new
    trade on a real qualifying signal. Live order execution is NOT done
    here (paper mode only).
    """
    conn = get_db()
    try:
        open_trade = conn.execute(
            "SELECT * FROM paper_trades WHERE user_id=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()

        if open_trade:
            token = open_trade["token"]
            symbol = open_trade["symbol"]
            exch_seg = open_trade["exch_seg"]
            if not token or not symbol or not exch_seg:
                return

            try:
                quote = obj.ltpData(exch_seg, symbol, token)
                current_ltp = float(quote["data"]["ltp"])
            except Exception:
                return

            trade_side = open_trade["side"]
            sl = open_trade["sl_price"]
            target = open_trade["target_price"]

            # Both CE and PE are bought options.
            # For either option type:
            # premium below SL = loss, premium above target = profit.
            hit_sl = bool(
                sl
                and current_ltp <= float(sl)
            )
            hit_target = bool(
                target
                and current_ltp >= float(target)
            )

            now_ist = (
                datetime.now(timezone.utc)
                + timedelta(hours=5, minutes=30)
            )
            force_eod_exit = (
                now_ist.hour * 60 + now_ist.minute
                >= 15 * 60 + 25
            )

            if hit_sl or hit_target or force_eod_exit:
                qty = open_trade["qty"] or 1
                entry_price = open_trade["entry_price"] or 0

                # Bought CE and bought PE both profit when their
                # respective option premium rises.
                pnl = round(
                    (current_ltp - entry_price) * qty,
                    2,
                )

                if hit_target:
                    reason = "TARGET HIT (real premium)"
                elif hit_sl:
                    reason = "SL HIT (real premium)"
                else:
                    reason = "EOD EXIT 15:25 IST"

                conn.execute(
                    "UPDATE paper_trades SET exit_price=?, pnl=?, status='CLOSED', reason=? WHERE id=?",
                    (current_ltp, pnl, reason, open_trade["id"])
                )
                conn.commit()
            return

        if not trade_allowed or side not in ("CE", "PE"):
            return
        if settings.get("trading_mode", "paper") != "paper":
            return

        # Never open a fresh paper trade at or after force-exit time.
        now_ist = (
            datetime.now(timezone.utc)
            + timedelta(hours=5, minutes=30)
        )
        if (
            now_ist.hour * 60 + now_ist.minute
            >= 15 * 60 + 25
        ):
            return

        resolved = resolve_option(underlying, price, side)
        if not resolved:
            return

        try:
            quote = obj.ltpData(resolved["exch_seg"], resolved["symbol"], resolved["token"])
            entry_price = float(quote["data"]["ltp"])
        except Exception:
            return

        if entry_price <= 0:
            return

        qty = LOT_SIZES.get(underlying, 1)

        sl_percent = float(
            settings.get("sl_percent", 12)
        )
        target_percent = float(
            settings.get("target_percent", 24)
        )

        reward_multiple = max(
            1.0,
            target_percent / max(sl_percent, 1.0),
        )

        # Strategy weekly expiry day: Tuesday.
        is_expiry_day = now_ist.weekday() == 1

        atr_levels = calculate_option_atr_levels(
            spot_price=price,
            option_entry_price=entry_price,
            spot_atr=spot_atr,
            is_expiry_day=is_expiry_day,
            sl_floor_percent=sl_percent,
            reward_multiple=reward_multiple,
        )

        sl_price = atr_levels["sl_price"]
        target_price = atr_levels["target_price"]

        conn.execute(
            """INSERT INTO paper_trades
               (user_id, symbol, side, entry_price, qty, pnl, status, reason,
                sl_price, target_price, token, exch_seg, expiry, strike, created_at)
               VALUES (?, ?, ?, ?, ?, 0, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                resolved["symbol"],
                side,
                entry_price,
                qty,
                (
                    f"Real entry score {score}"
                    f" | {atr_levels['mode']}"
                    f" | R={atr_levels['risk_points']}"
                    f" | RR={atr_levels['reward_multiple']}"
                ),
                sl_price,
                target_price,
                resolved["token"],
                resolved["exch_seg"],
                resolved["expiry"],
                resolved["strike"],
                datetime.now(timezone.utc).isoformat()
            )
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


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


def get_candles(obj, token: str, interval: str = "ONE_MINUTE", exchange: str = "NSE"):
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    from_dt = now_ist.replace(hour=9, minute=15,
                               second=0, microsecond=0)
    params = {
        "exchange": exchange,
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


def calculate_indicators(df):
    """EMA9, EMA21, VWAP, ATR, ADX, Volume ratio"""
    if df is None or len(df) < 21:
        return None

    df["EMA9"]  = df["close"].ewm(span=9,  adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()

    df["TP"] = (
        df["high"]
        + df["low"]
        + df["close"]
    ) / 3

    # Index historical/live candles may contain zero volume.
    # In that case normal VWAP becomes NaN because cumulative
    # weighted price is divided by zero.
    #
    # Use true volume-weighted VWAP whenever volume is available.
    # Otherwise use the expanding session average of typical price
    # as a safe VWAP fallback. This does not create fake volume.
    safe_volume = (
        df["volume"]
        .fillna(0)
        .clip(lower=0)
    )
    cumulative_volume = safe_volume.cumsum()
    cumulative_value = (
        df["TP"] * safe_volume
    ).cumsum()

    session_price_average = df["TP"].expanding(
        min_periods=1
    ).mean()

    volume_vwap = cumulative_value / cumulative_volume.where(
        cumulative_volume > 0
    )

    df["VWAP"] = volume_vwap.where(
        cumulative_volume > 0,
        session_price_average,
    )

    df["VWAP"] = (
        df["VWAP"]
        .ffill()
        .fillna(session_price_average)
    )

    df["VWAP_FALLBACK_USED"] = cumulative_volume <= 0

    df["H-L"]  = df["high"] - df["low"]
    df["H-PC"] = (df["high"] - df["close"].shift(1)).abs()
    df["L-PC"] = (df["low"]  - df["close"].shift(1)).abs()
    df["TR"]   = df[["H-L","H-PC","L-PC"]].max(axis=1)
    df["ATR"]  = df["TR"].ewm(span=14, adjust=False).mean()

    df["DM+"] = (df["high"] - df["high"].shift(1)).clip(lower=0)
    df["DM-"] = (df["low"].shift(1) - df["low"]).clip(lower=0)
    df["DI+"] = 100 * df["DM+"].ewm(span=14, adjust=False).mean() / df["ATR"]
    df["DI-"] = 100 * df["DM-"].ewm(span=14, adjust=False).mean() / df["ATR"]
    dx = (df["DI+"] - df["DI-"]).abs() / (df["DI+"] + df["DI-"] + 1e-9) * 100
    df["ADX"] = dx.ewm(span=14, adjust=False).mean()

    # Keep unavailable index volume neutral.
    # Zero volume receives no bonus but must not produce NaN/Infinity.
    df["VOL_MA"] = safe_volume.rolling(
        20,
        min_periods=1,
    ).mean()

    valid_volume_ma = df["VOL_MA"].where(
        df["VOL_MA"] > 0
    )

    df["VOL_RATIO"] = (
        safe_volume / valid_volume_ma
    ).fillna(0.0)

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

    last = df.iloc[-1]
    if last["EMA9"] > last["EMA21"]:
        trend = "UPTREND"
    elif last["EMA9"] < last["EMA21"]:
        trend = "DOWNTREND"
    else:
        trend = "SIDEWAYS"

    return df, trend


def run_user_bot(user_id: int, creds: dict, state: dict):
    """Runs in background thread for each user"""
    obj = None
    while state.get("running"):
        try:
            if obj is None:
                obj = angel_login(creds)
                state["status"] = "LOGGED_IN"

            settings = _read_settings(user_id)
            underlying = settings.get("primary_instrument", "NIFTY")
            if underlying not in INDEX_TOKENS:
                underlying = "NIFTY"
            token = INDEX_TOKENS[underlying]
            exchange = INDEX_EXCHANGE[underlying]

            df = get_candles(obj, token, exchange=exchange)
            if df is None or len(df) < 28:
                state["status"] = "WAITING_CANDLES"
                time.sleep(30)
                continue

            result = calculate_indicators(df)
            if result is None:
                time.sleep(30)
                continue

            df, trend = result
            last = df.iloc[-2]
            c1   = df.iloc[-3]
            c2   = df.iloc[-2]
            orb_high, orb_low = calculate_orb_levels(df)

            market_data = {
                "price":         float(last["close"]),
                "vwap":          float(last["VWAP"]),
                "ema9":          float(last["EMA9"]),
                "ema21":         float(last["EMA21"]),
                "adx":           float(last["ADX"]),
                "volume_ratio":  float(last["VOL_RATIO"]),
                "vwap_fallback_used": bool(
                    last["VWAP_FALLBACK_USED"]
                ),
                "supertrend_dir":str(last["ST_DIR"]),
                "trend":         trend,
                "mtf_confirmed": trend != "SIDEWAYS",
                "c1_bullish":    float(c1["close"]) > float(c1["open"]),
                "c2_bullish":    float(c2["close"]) > float(c2["open"]),
                "gap_day":       False,
                "orb_high":      orb_high,
                "orb_low":       orb_low,
                "atr":           float(last["ATR"]),
            }

            consecutive_losses = _get_consecutive_losses_today(user_id)
            signal_data = get_full_signal(
                market_data,
                consecutive_losses=consecutive_losses,
            )
            hero = is_hero_window_active()

            try:
                _manage_paper_trade(
                    user_id,
                    underlying,
                    market_data["price"],
                    signal_data.get("signal"),
                    signal_data.get("score"),
                    signal_data.get("trade_allowed"),
                    settings,
                    obj,
                    spot_atr=market_data["atr"],
                )
            except Exception:
                pass

            state.update({
                **signal_data,
                "hero": hero,
                "price": market_data["price"],
                "underlying": underlying,
                "status": "RUNNING",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        except Exception as e:
            obj = None
            state["status"] = f"ERROR: {str(e)[:100]}"
            time.sleep(60)

        time.sleep(60)


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


# ── Lightweight LTP-only session (separate from full bot loop) ────
SENSEX_TOKEN = "99919000"
INDEX_TRADING_SYMBOLS = {"NIFTY": NIFTY_SYMBOL, "BANKNIFTY": BANK_SYMBOL, "SENSEX": "SENSEX"}

_ltp_sessions = {}   # user_id -> SmartConnect obj
_ltp_lock = threading.Lock()


def _get_ltp_session(user_id, creds):
    with _ltp_lock:
        obj = _ltp_sessions.get(user_id)
    if obj is not None:
        return obj
    obj = angel_login(creds)
    with _ltp_lock:
        _ltp_sessions[user_id] = obj
    return obj


def get_index_quotes(user_id, creds):
    """Returns {"NIFTY": {"ltp":.., "status":"connected"}, ...}"""
    results = {}
    try:
        obj = _get_ltp_session(user_id, creds)
    except Exception as e:
        return {sym: {"ltp": None, "status": "not_connected", "error": str(e)} for sym in INDEX_TOKENS}

    for sym, token in INDEX_TOKENS.items():
        exch = INDEX_EXCHANGE[sym]
        tsym = INDEX_TRADING_SYMBOLS[sym]
        try:
            quote = obj.ltpData(exch, tsym, token)
            if quote.get("status"):
                results[sym] = {"ltp": float(quote["data"]["ltp"]), "status": "connected"}
            else:
                results[sym] = {"ltp": None, "status": "not_connected", "error": quote.get("message")}
        except Exception as e:
            with _ltp_lock:
                _ltp_sessions.pop(user_id, None)
            results[sym] = {"ltp": None, "status": "not_connected", "error": str(e)}
    return results


# ── Multi-broker index config (for signal-engine candle fetching) ──
from bot.brokers.factory import create_broker

ZERODHA_INDEX_TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105, "SENSEX": 265}
ZERODHA_INDEX_EXCHANGE = {"NIFTY": "NSE", "BANKNIFTY": "NSE", "SENSEX": "BSE"}
UPSTOX_INDEX_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank", "SENSEX": "BSE_INDEX|SENSEX"}


def get_candles_multi(broker_name, broker_obj, underlying):
    """Fetch and normalize candles (columns: time,open,high,low,close,volume) for non-Angel brokers."""
    import pandas as pd
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    from_dt = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)

    if broker_name == "zerodha":
        token = ZERODHA_INDEX_TOKENS[underlying]
        res = broker_obj.get_candles(
            symbol=token, interval="minute",
            from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=now_ist.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if not res.get("success"):
            raise RuntimeError(res.get("message", "Zerodha candle fetch failed"))
        rows = res.get("candles", [])
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df = df.rename(columns={"date": "time"})[["time", "open", "high", "low", "close", "volume"]]

    elif broker_name == "upstox":
        key = UPSTOX_INDEX_KEYS[underlying]
        res = broker_obj.get_candles(
            symbol=key, interval="1m",
            from_date=now_ist.strftime("%Y-%m-%d"),
            to_date=from_dt.strftime("%Y-%m-%d"),
        )
        if not res.get("success"):
            raise RuntimeError(res.get("message", "Upstox candle fetch failed"))
        rows = res.get("candles", [])
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "oi"])
        df = df[["time", "open", "high", "low", "close", "volume"]]
        df = df.iloc[::-1].reset_index(drop=True)  # Upstox returns newest-first
    else:
        raise ValueError(f"get_candles_multi: unsupported broker {broker_name}")

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna()
    return df if not df.empty else None


def run_user_bot_multi(user_id: int, broker_name: str, creds: dict, state: dict):
    """Generalized bot loop for Zerodha/Upstox (signal generation only, no real-premium paper trade)."""
    obj = None
    while state.get("running"):
        try:
            if obj is None:
                obj = create_broker(broker_name, creds["client_id"], creds["api_key"], creds["password"], creds.get("totp_secret"))
                login_result = obj.login()
                if not login_result.get("success"):
                    raise RuntimeError(login_result.get("message", "Login failed"))
                state["status"] = "LOGGED_IN"

            settings = _read_settings(user_id)
            underlying = settings.get("primary_instrument", "NIFTY")
            if underlying not in INDEX_TOKENS:
                underlying = "NIFTY"

            df = get_candles_multi(broker_name, obj, underlying)
            if df is None or len(df) < 28:
                state["status"] = "WAITING_CANDLES"
                time.sleep(30)
                continue

            result = calculate_indicators(df)
            if result is None:
                time.sleep(30)
                continue

            df, trend = result
            last = df.iloc[-2]
            c1 = df.iloc[-3]
            c2 = df.iloc[-2]
            orb_high, orb_low = calculate_orb_levels(df)

            market_data = {
                "price": float(last["close"]),
                "vwap": float(last["VWAP"]),
                "ema9": float(last["EMA9"]),
                "ema21": float(last["EMA21"]),
                "adx": float(last["ADX"]),
                "volume_ratio": float(last["VOL_RATIO"]),
                "vwap_fallback_used": bool(
                    last["VWAP_FALLBACK_USED"]
                ),
                "supertrend_dir": str(last["ST_DIR"]),
                "trend": trend,
                "mtf_confirmed": trend != "SIDEWAYS",
                "c1_bullish": float(c1["close"]) > float(c1["open"]),
                "c2_bullish": float(c2["close"]) > float(c2["open"]),
                "gap_day": False,
                "orb_high": orb_high,
                "orb_low": orb_low,
                "atr": float(last["ATR"]),
            }

            consecutive_losses = _get_consecutive_losses_today(user_id)
            signal_data = get_full_signal(
                market_data,
                consecutive_losses=consecutive_losses,
            )
            hero = is_hero_window_active()

            state.update({
                **signal_data,
                "hero": hero,
                "price": market_data["price"],
                "underlying": underlying,
                "status": "RUNNING",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        except Exception as e:
            obj = None
            state["status"] = f"ERROR: {str(e)[:100]}"
            time.sleep(60)

        time.sleep(60)


def start_user_bot_multi(user_id: int, broker_name: str, creds: dict) -> dict:
    with _lock:
        if user_id in _user_bots and _user_bots[user_id].get("running"):
            return {"success": False, "message": "Bot already running"}
        state = {
            "running": True, "status": "STARTING", "signal": "WAITING",
            "score": 0, "user_id": user_id, "broker": broker_name,
        }
        _user_bots[user_id] = state
        t = threading.Thread(target=run_user_bot_multi, args=(user_id, broker_name, creds, state), daemon=True)
        t.start()
        return {"success": True, "message": f"Bot started ({broker_name})"}
