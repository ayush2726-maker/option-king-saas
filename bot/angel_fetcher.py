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
from strategy.profile_engine import get_active_profile_config
from local_gateway.service import gateway_ready, queue_live_entry

# ── Per-user bot instances ────────────────────────────────
_user_bots = {}  # user_id -> bot state
_lock = threading.Lock()
_entry_guard_state = {}

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
        "sl_percent": 0,
        "target_percent": 0,
        "entry_threshold": 82,
        "live_lots": 1,
        "max_concurrent_trades": 1,
        "max_trades_per_day": 5,
        "different_index_required": True,
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



def _manage_live_gateway_entry(
    user_id,
    underlying,
    price,
    side,
    score,
    trade_allowed,
    settings,
    obj,
    spot_atr=0.0,
    market_data=None,
    candle_id=None,
):
    """Queue one live entry for execution by the owner's static-IP phone."""
    if not trade_allowed or side not in ("CE", "PE"):
        return {"queued": False, "reason": "NO_QUALIFYING_LIVE_SIGNAL"}

    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    minute = now_ist.hour * 60 + now_ist.minute
    if minute < 9 * 60 + 15 or minute >= 15 * 60 + 25:
        return {"queued": False, "reason": "LIVE_ENTRY_TIME_BLOCK"}

    ready, reason, status = gateway_ready(user_id)
    if not ready:
        _entry_guard_state[user_id] = {
            "allowed": False,
            "reason": reason,
            "gateway": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return {"queued": False, "reason": reason}

    resolved = resolve_option(underlying, price, side)
    if not resolved:
        return {"queued": False, "reason": "OPTION_CONTRACT_NOT_RESOLVED"}

    try:
        quote = obj.ltpData(
            resolved["exch_seg"],
            resolved["symbol"],
            resolved["token"],
        )
        expected_entry = float(quote["data"]["ltp"])
    except Exception as exc:
        return {"queued": False, "reason": f"OPTION_LTP_FAILED: {str(exc)[:100]}"}
    if expected_entry <= 0:
        return {"queued": False, "reason": "INVALID_OPTION_LTP"}

    quality = _option_entry_quality_angel(obj, resolved, expected_entry)
    if quality.get("allowed") is False:
        _entry_guard_state[user_id] = {
            **quality,
            "allowed": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return {"queued": False, "reason": quality.get("reason")}

    sl_percent = max(3.0, float(settings.get("sl_percent", 12) or 12))
    target_percent = max(5.0, float(settings.get("target_percent", 24) or 24))
    reward_multiple = max(1.0, target_percent / max(sl_percent, 1.0))
    atr_levels = calculate_option_atr_levels(
        spot_price=price,
        option_entry_price=expected_entry,
        spot_atr=spot_atr,
        is_expiry_day=now_ist.weekday() == 1,
        sl_floor_percent=sl_percent,
        reward_multiple=reward_multiple,
    )

    live_lots = max(1, min(int(settings.get("live_lots", 1) or 1), 10))
    quantity = int(LOT_SIZES.get(underlying, 1)) * live_lots
    safe_candle_id = str(
        candle_id
        or now_ist.replace(second=0, microsecond=0).isoformat()
    )
    idempotency_key = (
        f"LIVE_ENTRY:{user_id}:{underlying}:{resolved['symbol']}:{safe_candle_id}"
    )
    payload = {
        "underlying": underlying,
        "option_type": side,
        "symbol": resolved["symbol"],
        "symboltoken": str(resolved["token"]),
        "exchange": resolved["exch_seg"],
        "quantity": quantity,
        "lots": live_lots,
        "lot_size": int(LOT_SIZES.get(underlying, 1)),
        "expected_entry_price": round(expected_entry, 2),
        "sl_percent": sl_percent,
        "target_percent": target_percent,
        "sl_price": atr_levels.get("sl_price"),
        "target_price": atr_levels.get("target_price"),
        "force_exit_at": "15:25",
        "score": int(score or 0),
        "min_score": int((market_data or {}).get("signal_min_score") or 82),
        "spot_price": round(float(price or 0), 2),
        "spot_atr": round(float(spot_atr or 0), 4),
        "expiry": resolved.get("expiry"),
        "strike": resolved.get("strike"),
        "candle_id": safe_candle_id,
        "different_index_required": bool(
            settings.get("different_index_required", True)
        ),
        "strategy_mode": settings.get("mode", "default"),
        "execution_route": "OWNER_STATIC_IP_LOCAL_GATEWAY",
    }
    result = queue_live_entry(
        user_id,
        payload,
        idempotency_key,
        max_concurrent=int(settings.get("max_concurrent_trades", 1) or 1),
        max_trades_per_day=int(settings.get("max_trades_per_day", 5) or 5),
    )
    _entry_guard_state[user_id] = {
        "allowed": bool(result.get("queued")),
        "reason": result.get("reason"),
        "trade_id": result.get("trade_id"),
        "command_id": result.get("command_id"),
        "symbol": resolved["symbol"],
        "quantity": quantity,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return result

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
    market_data=None,
    candle_id=None,
):
    """
    Checks/manages paper trades or queues LIVE orders to the local static-IP gateway.
    fetched from the broker. Closes on real SL/target hit, opens a new
    trade on a real qualifying signal. Live order execution is NOT done
    here (paper mode only).
    """
    if str(settings.get("trading_mode", "paper")).lower() == "live":
        return _manage_live_gateway_entry(
            user_id=user_id,
            underlying=underlying,
            price=price,
            side=side,
            score=score,
            trade_allowed=trade_allowed,
            settings=settings,
            obj=obj,
            spot_atr=spot_atr,
            market_data=market_data,
            candle_id=candle_id,
        )

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


def build_chart_candles(
    df,
    limit: int = 390,
):
    # Convert current full-day 1-minute dataframe into
    # JSON-safe OHLC candles with indicator overlays.
    if df is None or df.empty:
        return []

    import math

    def safe_number(value):
        try:
            number = float(value)
            return (
                round(number, 4)
                if math.isfinite(number)
                else None
            )
        except Exception:
            return None

    result = []

    for _, row in df.tail(limit).iterrows():
        candle_time = row.get("time")

        try:
            candle_time = candle_time.isoformat()
        except Exception:
            candle_time = str(candle_time)

        direction = str(
            row.get("ST_DIR") or "NEUTRAL"
        ).upper()

        if direction == "UP":
            supertrend = safe_number(
                row.get("LOWER")
            )
        elif direction == "DOWN":
            supertrend = safe_number(
                row.get("UPPER")
            )
        else:
            supertrend = None

        result.append({
            "time": candle_time,
            "open": safe_number(row.get("open")),
            "high": safe_number(row.get("high")),
            "low": safe_number(row.get("low")),
            "close": safe_number(row.get("close")),
            "volume": safe_number(row.get("volume")),
            "ema9": safe_number(row.get("EMA9")),
            "ema21": safe_number(row.get("EMA21")),
            "vwap": safe_number(row.get("VWAP")),
            "supertrend": supertrend,
            "supertrend_dir": direction,
            "adx": safe_number(row.get("ADX")),
            "atr": safe_number(row.get("ATR")),
        })

    return [
        candle
        for candle in result
        if all(
            candle.get(field) is not None
            for field in (
                "open",
                "high",
                "low",
                "close",
            )
        )
    ]


def _normalize_option_candles(rows):
    normalized = []

    for row in rows or []:
        try:
            if isinstance(row, dict):
                timestamp = (
                    row.get("date")
                    or row.get("time")
                    or row.get("timestamp")
                    or ""
                )
                open_price = float(row.get("open"))
                high_price = float(row.get("high"))
                low_price = float(row.get("low"))
                close_price = float(row.get("close"))
            else:
                if len(row) < 5:
                    continue

                timestamp = row[0]
                open_price = float(row[1])
                high_price = float(row[2])
                low_price = float(row[3])
                close_price = float(row[4])

            if min(
                open_price,
                high_price,
                low_price,
                close_price,
            ) <= 0:
                continue

            normalized.append({
                "time": str(timestamp),
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
            })
        except Exception:
            continue

    normalized.sort(
        key=lambda candle: candle["time"]
    )

    return normalized


def _premium_entry_quality(
    rows,
    current_ltp,
):
    candles = _normalize_option_candles(
        rows
    )
    current = float(current_ltp or 0)

    if current <= 0:
        return {
            "allowed": False,
            "reason": "INVALID_OPTION_LTP",
            "current_ltp": current,
        }

    if len(candles) < 4:
        return {
            "allowed": True,
            "reason": "OPTION_CANDLES_INSUFFICIENT",
            "current_ltp": round(current, 2),
            "candle_count": len(candles),
        }

    recent = candles[-6:]
    closes = [
        candle["close"]
        for candle in recent
    ]
    highs = [
        candle["high"]
        for candle in recent
    ]

    oldest_close = max(
        0.05,
        closes[0],
    )
    recent_high = max(highs)

    sorted_closes = sorted(closes)
    middle = len(sorted_closes) // 2

    if len(sorted_closes) % 2:
        median_close = sorted_closes[
            middle
        ]
    else:
        median_close = (
            sorted_closes[middle - 1]
            + sorted_closes[middle]
        ) / 2

    latest = recent[-1]

    rise_pct = (
        current / oldest_close - 1
    ) * 100
    pullback_pct = (
        current / recent_high - 1
    ) * 100
    extension_pct = (
        current / max(0.05, median_close)
        - 1
    ) * 100

    latest_bearish = (
        latest["close"]
        < latest["open"]
    )

    spike_reversal = (
        rise_pct >= 10.0
        and pullback_pct <= -3.5
    )

    bearish_after_run = (
        rise_pct >= 8.0
        and latest_bearish
        and current
        <= latest["close"] * 1.005
    )

    extreme_extension = (
        extension_pct >= 12.0
    )

    blocked = (
        spike_reversal
        or bearish_after_run
        or extreme_extension
    )

    if spike_reversal:
        reason = "OPTION_SPIKE_REVERSING"
    elif bearish_after_run:
        reason = "OPTION_BEARISH_AFTER_RUN"
    elif extreme_extension:
        reason = "OPTION_PREMIUM_OVEREXTENDED"
    else:
        reason = "OPTION_PREMIUM_ENTRY_OK"

    return {
        "allowed": not blocked,
        "reason": reason,
        "current_ltp": round(current, 2),
        "candle_count": len(candles),
        "rise_pct": round(rise_pct, 2),
        "pullback_pct": round(
            pullback_pct,
            2,
        ),
        "extension_pct": round(
            extension_pct,
            2,
        ),
        "latest_bearish": bool(
            latest_bearish
        ),
    }


def _option_entry_quality_angel(
    obj,
    resolved,
    current_ltp,
):
    try:
        now_ist = (
            datetime.now(timezone.utc)
            + timedelta(
                hours=5,
                minutes=30,
            )
        )
        from_dt = now_ist.replace(
            hour=9,
            minute=15,
            second=0,
            microsecond=0,
        )

        response = obj.getCandleData({
            "exchange": resolved[
                "exch_seg"
            ],
            "symboltoken": str(
                resolved["token"]
            ),
            "interval": "ONE_MINUTE",
            "fromdate": from_dt.strftime(
                "%Y-%m-%d %H:%M"
            ),
            "todate": now_ist.strftime(
                "%Y-%m-%d %H:%M"
            ),
        })

        rows = (
            response.get("data", [])
            if isinstance(
                response,
                dict,
            )
            else []
        )

        return _premium_entry_quality(
            rows,
            current_ltp,
        )
    except Exception as exc:
        return {
            "allowed": True,
            "reason": "OPTION_GUARD_FETCH_WARNING",
            "warning": str(exc)[:120],
            "current_ltp": round(
                float(current_ltp or 0),
                2,
            ),
        }


def _option_entry_quality_multi(
    broker_name,
    obj,
    resolved,
    current_ltp,
):
    try:
        now_ist = (
            datetime.now(timezone.utc)
            + timedelta(
                hours=5,
                minutes=30,
            )
        )
        today = now_ist.strftime(
            "%Y-%m-%d"
        )

        if broker_name == "upstox":
            result = obj.get_candles(
                symbol=(
                    resolved.get("token")
                    or resolved["symbol"]
                ),
                interval="1m",
                from_date=today,
                to_date=today,
                exchange=resolved.get(
                    "exchange",
                    "NSE_FO",
                ),
            )
        elif broker_name == "zerodha":
            result = obj.get_candles(
                symbol=resolved.get(
                    "token"
                ),
                interval="minute",
                from_date=now_ist.replace(
                    hour=9,
                    minute=15,
                    second=0,
                    microsecond=0,
                ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                to_date=now_ist.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                exchange=resolved.get(
                    "exchange",
                    "NFO",
                ),
            )
        else:
            return {
                "allowed": True,
                "reason": (
                    "OPTION_GUARD_UNSUPPORTED_BROKER"
                ),
            }

        if (
            not isinstance(result, dict)
            or not result.get("success")
        ):
            return {
                "allowed": True,
                "reason": "OPTION_GUARD_FETCH_WARNING",
                "warning": str(
                    (result or {}).get(
                        "message",
                        "option candles unavailable",
                    )
                )[:120],
                "current_ltp": round(
                    float(current_ltp or 0),
                    2,
                ),
            }

        return _premium_entry_quality(
            result.get("candles", []),
            current_ltp,
        )
    except Exception as exc:
        return {
            "allowed": True,
            "reason": "OPTION_GUARD_FETCH_WARNING",
            "warning": str(exc)[:120],
            "current_ltp": round(
                float(current_ltp or 0),
                2,
            ),
        }


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
            chart_candles = build_chart_candles(
                df,
                limit=390,
            )
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
            active_profile = (
                get_active_profile_config(
                    user_id
                )
            )
            signal_data = get_full_signal(
                market_data,
                consecutive_losses=consecutive_losses,
                profile=active_profile,
            )
            signal_data.setdefault(
                "strategy_profile_key",
                active_profile.get(
                    "profile_key",
                    "okai_default_82",
                ),
            )
            signal_data.setdefault(
                "strategy_profile_name",
                active_profile.get(
                    "profile_name",
                    "OKAI Default 82",
                ),
            )
            signal_data[
                "strategy_profile_locked"
            ] = bool(
                active_profile.get(
                    "profile_locked",
                    False,
                )
            )
            hero = is_hero_window_active()

            market_data["signal"] = signal_data.get(
                "signal",
                "WAIT",
            )
            market_data["signal_score"] = signal_data.get(
                "score",
                0,
            )
            market_data["signal_min_score"] = signal_data.get(
                "min_score",
                82,
            )

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
                    market_data=market_data,
                    candle_id=str(last["time"]),
                )
            except Exception:
                pass

            state.update({
                **signal_data,
                "hero": hero,
                "price": market_data["price"],
                "underlying": underlying,
                "chart_instrument": underlying,
                "chart_interval": "ONE_MINUTE",
                "chart_candles": chart_candles,
                "entry_guard": _entry_guard_state.get(
                    user_id
                ),
                "open_trade_monitor_seconds": 5,
                "status": "RUNNING",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

            # Open position premium ko 5-second interval par monitor karo.
            # WAIT/False isliye pass hota hai taaki exit ke baad isi old
            # signal par accidental re-entry na ho.
            for _ in range(12):
                time.sleep(5)
                if not state.get("running"):
                    break
                try:
                    _manage_paper_trade(
                        user_id,
                        underlying,
                        market_data["price"],
                        "WAIT",
                        0,
                        False,
                        settings,
                        obj,
                        spot_atr=market_data["atr"],
                        market_data=market_data,
                        candle_id=str(last["time"]),
                    )
                except Exception:
                    pass
            continue

        except Exception as e:
            obj = None
            state["status"] = f"ERROR: {str(e)[:100]}"
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
            chart_candles = build_chart_candles(
                df,
                limit=390,
            )
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
            active_profile = (
                get_active_profile_config(
                    user_id
                )
            )
            signal_data = get_full_signal(
                market_data,
                consecutive_losses=consecutive_losses,
                profile=active_profile,
            )
            signal_data.setdefault(
                "strategy_profile_key",
                active_profile.get(
                    "profile_key",
                    "okai_default_82",
                ),
            )
            signal_data.setdefault(
                "strategy_profile_name",
                active_profile.get(
                    "profile_name",
                    "OKAI Default 82",
                ),
            )
            signal_data[
                "strategy_profile_locked"
            ] = bool(
                active_profile.get(
                    "profile_locked",
                    False,
                )
            )
            hero = is_hero_window_active()

            market_data["signal"] = signal_data.get(
                "signal",
                "WAIT",
            )
            market_data["signal_score"] = signal_data.get(
                "score",
                0,
            )
            market_data["signal_min_score"] = signal_data.get(
                "min_score",
                82,
            )

            try:
                _manage_paper_trade_multi(
                    user_id, broker_name, underlying,
                    market_data["price"],
                    signal_data.get("signal"),
                    signal_data.get("score"),
                    signal_data.get("trade_allowed"),
                    settings, obj,
                    spot_atr=market_data["atr"],
                    market_data=market_data,
                    candle_id=str(last["time"]),
                )
            except Exception:
                pass

            state.update({
                **signal_data,
                "hero": hero,
                "price": market_data["price"],
                "underlying": underlying,
                "chart_instrument": underlying,
                "chart_interval": "ONE_MINUTE",
                "chart_candles": chart_candles,
                "entry_guard": _entry_guard_state.get(
                    user_id
                ),
                "open_trade_monitor_seconds": 5,
                "status": "RUNNING",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

            # Open position premium ko 5-second interval par monitor karo.
            # WAIT/False isliye pass hota hai taaki exit ke baad isi old
            # signal par accidental re-entry na ho.
            for _ in range(12):
                time.sleep(5)
                if not state.get("running"):
                    break
                try:
                    _manage_paper_trade_multi(
                        user_id,
                        broker_name,
                        underlying,
                        market_data["price"],
                        "WAIT",
                        0,
                        False,
                        settings,
                        obj,
                        spot_atr=market_data["atr"],
                        market_data=market_data,
                        candle_id=str(last["time"]),
                    )
                except Exception:
                    pass
            continue

        except Exception as e:
            obj = None
            state["status"] = f"ERROR: {str(e)[:100]}"
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


# ============================================================
# Broker-neutral pure ATR paper exit engine (V2)
# ============================================================
from bot.dynamic_exit import (
    calculate_option_atr_levels as _dynamic_atr_levels,
    update_option_profit_lock as _dynamic_profit_lock,
    detect_structural_reversal as _dynamic_structural_reversal,
)
from bot.option_chain import get_atm_strike as _dynamic_atm_strike


def _dv(row, key, default=None):
    try:
        if key in row.keys() and row[key] is not None:
            return row[key]
    except Exception:
        pass
    return default


def _dynamic_reversal_state(
    trade,
    market_data,
    candle_id,
):
    old_count = int(_dv(trade, "reversal_count", 0))
    old_candle = str(
        _dv(trade, "reversal_last_candle", "")
        or ""
    )

    if not market_data or candle_id is None:
        return {
            "count": old_count,
            "last_candle": old_candle,
            "exit": old_count >= 2,
            "details": {"detected": False},
        }

    details = _dynamic_structural_reversal(
        position_side=trade["side"],
        price=market_data.get("price"),
        vwap=market_data.get("vwap"),
        ema9=market_data.get("ema9"),
        ema21=market_data.get("ema21"),
        supertrend_dir=market_data.get(
            "supertrend_dir"
        ),
        opposite_signal=market_data.get(
            "signal",
            "WAIT",
        ),
        opposite_score=market_data.get(
            "signal_score",
            0,
        ),
        min_score=market_data.get(
            "signal_min_score",
            82,
        ),
    )

    current_candle = str(candle_id)

    if current_candle != old_candle:
        count = (
            old_count + 1
            if details["detected"]
            else 0
        )
        last_candle = current_candle
    else:
        count = old_count
        last_candle = old_candle

    return {
        "count": count,
        "last_candle": last_candle,
        "exit": count >= 2,
        "details": details,
    }


def _dynamic_manage_open(
    conn,
    trade,
    ltp,
    market_data=None,
    candle_id=None,
):
    entry = float(trade["entry_price"] or 0)
    qty = int(trade["qty"] or 1)
    old_sl = float(_dv(trade, "sl_price", max(0.05, entry-0.05)))
    risk = float(_dv(trade, "initial_risk", max(0.05, entry-old_sl)))
    peak = float(_dv(trade, "peak_price", entry))
    updates = int(_dv(trade, "trail_updates", 0))

    trail = _dynamic_profit_lock(entry, risk, old_sl, peak, ltp)
    if trail["updated"]:
        updates += 1

    reversal = _dynamic_reversal_state(
        trade,
        market_data,
        candle_id,
    )
    reversal_exit = reversal["exit"]

    conn.execute(
        """UPDATE paper_trades
           SET sl_price=?, target_price=NULL, initial_risk=?,
               peak_price=?, trail_stage=?, trail_updates=?,
               last_ltp=?, reversal_count=?,
               reversal_last_candle=?
           WHERE id=?""",
        (
            trail["sl_price"], risk, trail["peak_price"],
            trail["stage"], updates, ltp,
            reversal["count"],
            reversal["last_candle"],
            trade["id"],
        ),
    )

    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    eod = now_ist.hour * 60 + now_ist.minute >= 15 * 60 + 25
    hit = ltp <= trail["sl_price"]
    if not hit and not reversal_exit and not eod:
        conn.commit()
        return

    pnl = round((ltp-entry)*qty, 2)
    if hit and trail["sl_price"] >= entry:
        reason = (
            "PROFIT LOCK TRAIL HIT"
            f" | {trail['stage']}"
            f" | locked={trail['locked_r']}R"
        )
    elif hit:
        reason = "PURE ATR SL HIT"
    elif reversal_exit:
        reason = (
            "TWO CANDLE STRUCTURAL REVERSAL EXIT"
            f" | count={reversal['count']}"
        )
    else:
        reason = "EOD EXIT 15:25 IST"

    conn.execute(
        """UPDATE paper_trades
           SET exit_price=?, pnl=?, status='CLOSED',
               reason=?, last_ltp=?
           WHERE id=?""",
        (ltp, pnl, reason, ltp, trade["id"]),
    )
    conn.commit()


def _dynamic_insert_trade(
    conn, user_id, broker_name, resolved, side,
    entry, qty, score, spot_price, spot_atr,
):
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    levels = _dynamic_atr_levels(
        spot_price, entry, spot_atr,
        is_expiry_day=now_ist.weekday() == 1,
    )
    if not levels["atr_available"]:
        return

    risk = levels["risk_points"]
    conn.execute(
        """INSERT INTO paper_trades
           (
             user_id,symbol,side,entry_price,qty,pnl,status,
             reason,sl_price,target_price,token,exch_seg,
             expiry,strike,created_at,initial_risk,
             peak_price,trail_stage,trail_updates,last_ltp,
             broker_name
           )
           VALUES (
             ?,?,?,?,?,0,'OPEN',?,?,NULL,?,?,?,?,?,?,?,
             'INITIAL_ATR',0,?,?
           )""",
        (
            user_id, resolved["symbol"], side, entry, qty,
            (
                f"Real entry score {score}"
                f" | {levels['mode']} | R={risk}"
                " | DYNAMIC_PROFIT_LOCK"
            ),
            levels["sl_price"], resolved.get("token"),
            resolved.get("exchange") or resolved.get("exch_seg"),
            resolved.get("expiry"), resolved.get("strike"),
            datetime.now(timezone.utc).isoformat(),
            risk, entry, entry, broker_name,
        ),
    )
    conn.commit()


def _manage_paper_trade(
    user_id, underlying, price, side, score,
    trade_allowed, settings, obj, spot_atr=0.0,
    market_data=None, candle_id=None,
):
    if settings.get("trading_mode", "paper") != "paper":
        return
    conn = get_db()
    try:
        trade = conn.execute(
            """SELECT * FROM paper_trades
               WHERE user_id=? AND status='OPEN'
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        ).fetchone()

        if trade:
            try:
                q = obj.ltpData(
                    trade["exch_seg"], trade["symbol"], trade["token"]
                )
                ltp = float(q["data"]["ltp"])
            except Exception:
                return
            _dynamic_manage_open(
                conn,
                trade,
                ltp,
                market_data=market_data,
                candle_id=candle_id,
            )
            return

        if not trade_allowed or side not in ("CE","PE") or spot_atr <= 0:
            return
        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        if now_ist.hour*60 + now_ist.minute >= 15*60+25:
            return

        resolved = resolve_option(underlying, price, side)
        if not resolved:
            return
        resolved = dict(resolved)
        resolved["exchange"] = resolved.get("exch_seg")
        try:
            q = obj.ltpData(
                resolved["exch_seg"], resolved["symbol"], resolved["token"]
            )
            entry = float(q["data"]["ltp"])
        except Exception:
            return
        if entry <= 0:
            return

        entry_quality = (
            _option_entry_quality_angel(
                obj,
                resolved,
                entry,
            )
        )
        _entry_guard_state[
            user_id
        ] = entry_quality

        if not entry_quality.get(
            "allowed",
            True,
        ):
            return

        _dynamic_insert_trade(
            conn, user_id, "angelone", resolved, side, entry,
            LOT_SIZES.get(underlying,1), score, price, spot_atr,
        )
    finally:
        conn.close()


def _manage_paper_trade_multi(
    user_id, broker_name, underlying, price, side,
    score, trade_allowed, settings, obj, spot_atr=0.0,
    market_data=None, candle_id=None,
):
    if settings.get("trading_mode", "paper") != "paper":
        return
    conn = get_db()
    try:
        trade = conn.execute(
            """SELECT * FROM paper_trades
               WHERE user_id=? AND status='OPEN'
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        ).fetchone()

        if trade:
            if broker_name == "upstox":
                ref = _dv(trade, "token", trade["symbol"])
                q = obj.get_ltp(ref, exchange=_dv(trade,"exch_seg","NSE_FO"))
            else:
                q = obj.get_ltp(
                    trade["symbol"],
                    exchange=_dv(trade,"exch_seg","NFO"),
                )
            if not q.get("success"):
                return
            _dynamic_manage_open(
                conn,
                trade,
                float(q["ltp"]),
                market_data=market_data,
                candle_id=candle_id,
            )
            return

        if not trade_allowed or side not in ("CE","PE") or spot_atr <= 0:
            return
        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        if now_ist.hour*60 + now_ist.minute >= 15*60+25:
            return

        resolved = obj.search_option(
            underlying, "current_week",
            _dynamic_atm_strike(underlying, price), side,
        )
        if not resolved.get("success"):
            return

        if broker_name == "upstox":
            q = obj.get_ltp(
                resolved.get("token") or resolved["symbol"],
                exchange=resolved.get("exchange","NSE_FO"),
            )
        else:
            q = obj.get_ltp(
                resolved["symbol"],
                exchange=resolved.get("exchange","NFO"),
            )
        if not q.get("success"):
            return
        entry = float(q["ltp"])
        if entry <= 0:
            return

        entry_quality = (
            _option_entry_quality_multi(
                broker_name,
                obj,
                resolved,
                entry,
            )
        )
        _entry_guard_state[
            user_id
        ] = entry_quality

        if not entry_quality.get(
            "allowed",
            True,
        ):
            return

        qty = int(
            resolved.get("lot_size")
            or LOT_SIZES.get(underlying,1)
        )
        _dynamic_insert_trade(
            conn, user_id, broker_name, resolved, side, entry,
            qty, score, price, spot_atr,
        )
    finally:
        conn.close()


# ============================================================
# AUTO three-index portfolio runtime:
# 50% first slot + 40% second slot + 10% reserve.
# This late import intentionally replaces the legacy
# single-index loops while retaining tested helpers.
# ============================================================
from bot.auto_portfolio_runtime import (
    run_user_bot_auto as run_user_bot,
    run_user_bot_multi_auto as run_user_bot_multi,
)

