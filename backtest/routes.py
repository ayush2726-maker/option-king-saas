from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from auth.utils import decrypt_credential
from datetime import datetime, timezone, timedelta
import json
import math
import threading
import uuid

try:
    from telegram.routes import notify_user
except Exception:
    def notify_user(user_id, msg):
        return None

try:
    from bot.angel_fetcher import (
        angel_login, calculate_indicators, calculate_orb_levels,
        INDEX_TOKENS, INDEX_EXCHANGE,
        ZERODHA_INDEX_TOKENS, ZERODHA_INDEX_EXCHANGE, UPSTOX_INDEX_KEYS,
    )
    from bot.strategy import get_full_signal
    from bot.dynamic_exit import (
        calculate_option_atr_levels,
        update_option_profit_lock,
        detect_structural_reversal,
    )
    from bot.brokers.factory import create_broker
    ENGINE_AVAILABLE = True
except Exception:
    ENGINE_AVAILABLE = False


router = APIRouter(prefix="/backtest", tags=["Backtest"])

LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}


# Cache completed historical days. COMBINED first runs NORMAL
# and then HERO ZERO for the same instrument/date candles.
_OKAI_BACKTEST_CANDLE_CACHE = {}
_OKAI_BACKTEST_CANDLE_CACHE_MAX = 96


def _okai_is_completed_historical_date(date_str):
    try:
        requested = datetime.strptime(
            str(date_str),
            "%Y-%m-%d",
        ).date()
        ist_now = (
            datetime.now(timezone.utc)
            + timedelta(hours=5, minutes=30)
        )
        return requested < ist_now.date()
    except Exception:
        return False


def _okai_candle_cache_key(
    broker_name,
    instrument,
    date_str,
):
    return (
        str(broker_name or "").lower(),
        str(instrument or "").upper(),
        str(date_str or ""),
    )


def _okai_get_cached_candles(
    broker_name,
    instrument,
    date_str,
):
    if not _okai_is_completed_historical_date(
        date_str
    ):
        return None

    key = _okai_candle_cache_key(
        broker_name,
        instrument,
        date_str,
    )
    cached = _OKAI_BACKTEST_CANDLE_CACHE.get(
        key
    )

    if cached is None:
        return None

    return cached.copy(deep=True)


def _okai_store_cached_candles(
    broker_name,
    instrument,
    date_str,
    dataframe,
):
    if not _okai_is_completed_historical_date(
        date_str
    ):
        return

    if dataframe is None or dataframe.empty:
        return

    key = _okai_candle_cache_key(
        broker_name,
        instrument,
        date_str,
    )

    _OKAI_BACKTEST_CANDLE_CACHE[
        key
    ] = dataframe.copy(deep=True)

    while (
        len(_OKAI_BACKTEST_CANDLE_CACHE)
        > _OKAI_BACKTEST_CANDLE_CACHE_MAX
    ):
        oldest_key = next(
            iter(_OKAI_BACKTEST_CANDLE_CACHE)
        )
        _OKAI_BACKTEST_CANDLE_CACHE.pop(
            oldest_key,
            None,
        )


def _okai_trend_from_indicator_row(row):
    try:
        ema9 = float(row["EMA9"])
        ema21 = float(row["EMA21"])

        if ema9 > ema21:
            return "UPTREND"
        if ema9 < ema21:
            return "DOWNTREND"
    except Exception:
        pass

    return "SIDEWAYS"


def _okai_precompute_indicators(dataframe):
    # Indicators are causal, so one complete-day calculation
    # gives the same value at each historical candle.
    if dataframe is None or dataframe.empty:
        return None

    calculated = calculate_indicators(
        dataframe.copy(deep=True)
    )

    if calculated is None:
        return None

    indicator_df, _ = calculated
    return indicator_df


def _candle_minutes_ist(value):
    """Return candle time as IST minutes from midnight."""
    try:
        text = str(value).strip().replace("Z", "+00:00")
        candle_dt = datetime.fromisoformat(text)

        if candle_dt.tzinfo is not None:
            ist = timezone(timedelta(hours=5, minutes=30))
            candle_dt = candle_dt.astimezone(ist)

        return candle_dt.hour * 60 + candle_dt.minute

    except Exception:
        return -1


def _json_safe(value, stats=None):
    """
    Convert NaN, Infinity, numpy scalars and other values into
    strict JSON-compatible Python values.
    """
    if stats is None:
        stats = {"non_finite": 0}

    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        if math.isfinite(value):
            return value

        stats["non_finite"] = stats.get("non_finite", 0) + 1
        return None

    # numpy/pandas scalar values
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item(), stats)
        except Exception:
            pass

    if isinstance(value, dict):
        return {
            str(key): _json_safe(item, stats)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            _json_safe(item, stats)
            for item in value
        ]

    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass

    return str(value)


def ensure_backtest_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 0,
            instrument TEXT,
            run_date TEXT,
            capital REAL DEFAULT 100000,
            entry_score INTEGER DEFAULT 82,
            sl_percent REAL DEFAULT 12,
            target_percent REAL DEFAULT 24,
            result_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    existing = {row[1] for row in conn.execute("PRAGMA table_info(backtest_runs)").fetchall()}

    required = {
        "user_id": "INTEGER DEFAULT 0",
        "instrument": "TEXT",
        "run_date": "TEXT",
        "capital": "REAL DEFAULT 100000",
        "entry_score": "INTEGER DEFAULT 82",
        "sl_percent": "REAL DEFAULT 12",
        "target_percent": "REAL DEFAULT 24",
        "result_json": "TEXT",
        "created_at": "TEXT"
    }

    for col, typ in required.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE backtest_runs ADD COLUMN {col} {typ}")

    conn.commit()


def is_weekend(date_str):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.weekday() >= 5
    except Exception:
        return False


def fetch_backtest_candles(broker_name, obj, instrument, date_str):
    """Fetch a day's 5-min candles for any supported broker, normalized to time/open/high/low/close/volume."""
    import pandas as pd

    cached = _okai_get_cached_candles(
        broker_name,
        instrument,
        date_str,
    )
    if cached is not None:
        return cached

    day = datetime.strptime(date_str, "%Y-%m-%d")
    from_dt = day.replace(hour=9, minute=15, second=0, microsecond=0)
    to_dt = day.replace(hour=15, minute=30, second=0, microsecond=0)

    if broker_name == "angelone":
        token = INDEX_TOKENS.get(instrument, "26000")
        exchange = INDEX_EXCHANGE.get(instrument, "NSE")
        params = {
            "exchange": exchange, "symboltoken": token, "interval": "FIVE_MINUTE",
            "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"), "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        data = obj.getCandleData(params)
        rows = data.get("data", []) if isinstance(data, dict) else []
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])

    elif broker_name == "zerodha":
        token = ZERODHA_INDEX_TOKENS.get(instrument)
        res = obj.get_candles(
            symbol=token, interval="5minute",
            from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=to_dt.strftime("%Y-%m-%d %H:%M:%S"),
        )
        rows = res.get("candles", []) if res.get("success") else []
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.rename(columns={"date": "time"})[["time", "open", "high", "low", "close", "volume"]]

    elif broker_name == "upstox":
        key = UPSTOX_INDEX_KEYS.get(instrument)
        res = obj.get_candles(symbol=key, interval="1m", from_date=date_str, to_date=date_str)
        rows = res.get("candles", []) if res.get("success") else []
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "oi"]) if rows else pd.DataFrame()
        if not df.empty:
            df = df[["time", "open", "high", "low", "close", "volume"]].iloc[::-1].reset_index(drop=True)
    else:
        df = pd.DataFrame()

    if df.empty:
        return None

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    clean_df = df.dropna().reset_index(
        drop=True
    )

    _okai_store_cached_candles(
        broker_name,
        instrument,
        date_str,
        clean_df,
    )

    return clean_df.copy(deep=True)


def run_realistic_day_backtest(broker_name, obj, instrument, date_str, capital, entry_threshold, sl_percent, target_percent):
    qty = LOT_SIZES.get(instrument, 65)

    try:
        df = fetch_backtest_candles(broker_name, obj, instrument, date_str)
    except Exception as e:
        return {"success": False, "message": f"Historical data fetch failed: {str(e)[:150]}"}

    if df is None or df.empty:
        return {"success": False, "message": "No data found for this date (market holiday or no historical data)."}

    if len(df) < 30:
        return {"success": False, "message": "Insufficient candle data for this date."}

    full_indicator_df = _okai_precompute_indicators(
        df
    )
    if full_indicator_df is None:
        return {
            "success": False,
            "message": (
                "Unable to calculate indicators "
                "for this date."
            ),
        }

    trades = []
    open_trade = None
    trade_no = 0
    consecutive_losses = 0
    _score_log = []
    _score_detail_log = []

    try:
        is_expiry_day = (
            datetime.fromisoformat(date_str).weekday()
            == 1
        )
    except Exception:
        is_expiry_day = False

    for i in range(
        28,
        len(full_indicator_df),
    ):
        wdf = full_indicator_df.iloc[
            :i + 1
        ]
        last = wdf.iloc[-1]
        trend = _okai_trend_from_indicator_row(
            last
        )
        c1 = wdf.iloc[-3] if len(wdf) >= 3 else wdf.iloc[-1]
        c2 = wdf.iloc[-2] if len(wdf) >= 2 else wdf.iloc[-1]

        price = float(last["close"])

        candle_minutes = _candle_minutes_ist(last["time"])
        force_eod_exit = (
            candle_minutes >= 15 * 60 + 25
        )

        if open_trade:
            side = open_trade["side"]
            entry = open_trade["entry_price"]
            entry_spot = open_trade["entry_spot"]
            spot_close = float(last["close"])
            spot_high = float(last["high"])
            spot_low = float(last["low"])

            close_pct = (spot_close-entry_spot)/entry_spot*100
            if side == "CE":
                good_pct = (spot_high-entry_spot)/entry_spot*100
                bad_pct = (spot_low-entry_spot)/entry_spot*100
            else:
                close_pct = -close_pct
                good_pct = (entry_spot-spot_low)/entry_spot*100
                bad_pct = (entry_spot-spot_high)/entry_spot*100

            response = 8.0
            current_premium = max(0.5, entry*(1+close_pct*response/100))
            premium_high = max(0.5, entry*(1+good_pct*response/100))
            premium_low = max(0.5, entry*(1+bad_pct*response/100))

            active_sl = open_trade["sl_price"]
            hit_sl = premium_low <= active_sl
            is_last = i == len(df)-1

            orb_high_open, orb_low_open = (
                calculate_orb_levels(wdf)
            )
            open_market_data = {
                "price": spot_close,
                "vwap": float(last["VWAP"]),
                "ema9": float(last["EMA9"]),
                "ema21": float(last["EMA21"]),
                "adx": float(last["ADX"]),
                "volume_ratio": float(
                    last["VOL_RATIO"]
                ),
                "vwap_fallback_used": bool(
                    last["VWAP_FALLBACK_USED"]
                ),
                "supertrend_dir": str(
                    last["ST_DIR"]
                ),
                "trend": trend,
                "mtf_confirmed": (
                    trend != "SIDEWAYS"
                ),
                "c1_bullish": (
                    float(c1["close"])
                    > float(c1["open"])
                ),
                "c2_bullish": (
                    float(c2["close"])
                    > float(c2["open"])
                ),
                "gap_day": False,
                "orb_high": orb_high_open,
                "orb_low": orb_low_open,
                "atr": float(last["ATR"]),
            }

            opposite_signal_data = get_full_signal(
                open_market_data,
                consecutive_losses=consecutive_losses,
            )

            reversal = detect_structural_reversal(
                position_side=side,
                price=spot_close,
                vwap=float(last["VWAP"]),
                ema9=float(last["EMA9"]),
                ema21=float(last["EMA21"]),
                supertrend_dir=str(last["ST_DIR"]),
                opposite_signal=opposite_signal_data.get(
                    "signal",
                    "WAIT",
                ),
                opposite_score=opposite_signal_data.get(
                    "score",
                    0,
                ),
                min_score=82,
            )

            if reversal["detected"]:
                open_trade["reversal_count"] += 1
            else:
                open_trade["reversal_count"] = 0

            open_trade["reversal_details"] = reversal
            structural_exit = (
                open_trade["reversal_count"] >= 2
            )

            if (
                hit_sl
                or structural_exit
                or force_eod_exit
                or is_last
            ):
                if hit_sl:
                    exit_price = round(active_sl,2)
                    reason = (
                        "PROFIT_LOCK_TRAIL"
                        if active_sl >= entry
                        else "PURE_ATR_SL"
                    )
                elif structural_exit:
                    exit_price = round(current_premium, 2)
                    reason = "TWO_CANDLE_REVERSAL_EXIT"
                else:
                    exit_price = round(current_premium,2)
                    reason = "EOD_EXIT_1525" if force_eod_exit else "DAY_END_EXIT"

                pnl = round((exit_price-entry)*qty,2)
                trades.append({
                    "trade_no": trade_no,
                    "symbol": f"{instrument} {side}",
                    "side": side,
                    "qty": qty,
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "pnl": pnl,
                    "reason": reason,
                    "score": open_trade["score"],
                    "entry_time": open_trade["entry_time"],
                    "exit_time": str(last["time"]),
                    "initial_sl_price": open_trade["initial_sl_price"],
                    "sl_price": active_sl,
                    "target_price": None,
                    "risk_points": open_trade["risk_points"],
                    "atr_mode": open_trade["atr_mode"],
                    "spot_atr_at_entry": open_trade["spot_atr_at_entry"],
                    "estimated_option_atr": open_trade["estimated_option_atr"],
                    "peak_price": open_trade["peak_price"],
                    "peak_r": open_trade["peak_r"],
                    "trail_stage": open_trade["trail_stage"],
                    "trail_updates": open_trade["trail_updates"],
                    "reversal_count": open_trade[
                        "reversal_count"
                    ],
                    "reversal_details": open_trade[
                        "reversal_details"
                    ],
                    "estimated_premium_high": round(premium_high,2),
                    "estimated_premium_low": round(premium_low,2),
                    "premium_response_factor": response,
                    "fixed_target_enabled": False,
                })
                consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0
                open_trade = None
                continue

            trail = update_option_profit_lock(
                entry,
                open_trade["risk_points"],
                active_sl,
                open_trade["peak_price"],
                premium_high,
            )
            if trail["updated"]:
                open_trade["trail_updates"] += 1
            open_trade["sl_price"] = trail["sl_price"]
            open_trade["peak_price"] = trail["peak_price"]
            open_trade["peak_r"] = trail["peak_r"]
            open_trade["trail_stage"] = trail["stage"]
            continue

        # Do not open another trade after the force-exit time.
        if force_eod_exit:
            continue

        orb_high, orb_low = calculate_orb_levels(wdf)

        market_data = {
            "price": price,
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

        signal_data = get_full_signal(
            market_data,
            consecutive_losses=consecutive_losses,
        )
        _score_log.append(signal_data.get("score", 0))
        _score_detail_log.append({
            "time": str(last["time"]),
            "candidate_signal": signal_data.get("candidate_signal"),
            "final_signal": signal_data.get("signal"),
            "score": signal_data.get("score", 0),
            "base_score": signal_data.get("base_score", 0),
            "ce_raw_score": signal_data.get("ce_raw_score", 0),
            "pe_raw_score": signal_data.get("pe_raw_score", 0),
            "adx": round(signal_data.get("adx", 0), 2),
            "adx_bonus": signal_data.get("adx_bonus", 0),
            "volume_ratio": round(
                signal_data.get("volume_ratio", 0),
                2,
            ),
            "volume_bonus": signal_data.get("volume_bonus", 0),
            "mtf_bonus": signal_data.get("mtf_bonus", 0),
            "orb_high": round(orb_high, 2),
            "orb_low": round(orb_low, 2),
            "ema_stretch_points": signal_data.get(
                "ema_stretch_points",
                0,
            ),
            "vwap_stretch_points": signal_data.get(
                "vwap_stretch_points",
                0,
            ),
            "vwap_fallback_used": signal_data.get(
                "vwap_fallback_used",
                False,
            ),
            "vwap_chase_enabled": signal_data.get(
                "vwap_chase_enabled",
                True,
            ),
            "ema_chase_blocked": signal_data.get(
                "ema_chase_blocked",
                False,
            ),
            "vwap_chase_blocked": signal_data.get(
                "vwap_chase_blocked",
                False,
            ),
            "chase_blocked": signal_data.get(
                "chase_blocked",
                False,
            ),
            "warnings": signal_data.get("warnings", []),
        })

        if signal_data["trade_allowed"] and signal_data["signal"] in ("CE", "PE") and signal_data["score"] >= entry_threshold:
            trade_no += 1
            atr = market_data["atr"]
            est_entry_premium = round(
                max(20, atr * 6),
                2,
            )

            atr_levels = calculate_option_atr_levels(
                spot_price=price,
                option_entry_price=est_entry_premium,
                spot_atr=atr,
                is_expiry_day=is_expiry_day,
            )

            open_trade = {
                "side": signal_data["signal"],
                "entry_price": est_entry_premium,
                "entry_spot": price,
                "score": signal_data["score"],
                "entry_time": str(last["time"]),
                "initial_sl_price": atr_levels["sl_price"],
                "sl_price": atr_levels["sl_price"],
                "target_price": None,
                "risk_points": atr_levels["risk_points"],
                "atr_mode": atr_levels["mode"],
                "spot_atr_at_entry": atr_levels["spot_atr"],
                "estimated_option_atr": atr_levels["estimated_option_atr"],
                "peak_price": est_entry_premium,
                "peak_r": 0.0,
                "trail_stage": "INITIAL_ATR",
                "trail_updates": 0,
                "reversal_count": 0,
                "reversal_details": {
                    "detected": False,
                },
            }

    total_pnl = round(sum(t["pnl"] for t in trades), 2)
    wins = sum(1 for t in trades if t["pnl"] >= 0)
    losses = len(trades) - wins
    win_rate = round((wins / len(trades)) * 100, 2) if trades else 0

    top_candidates = sorted(
        _score_detail_log,
        key=lambda row: row["score"],
        reverse=True,
    )[:10]

    return {
        "success": True,
        "instrument": instrument,
        "date": date_str,
        "capital": capital,
        "ending_capital": round(capital + total_pnl, 2),
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "trades": trades,
        "debug_max_score": max(_score_log) if _score_log else None,
        "debug_avg_score": round(sum(_score_log)/len(_score_log), 1) if _score_log else None,
        "debug_score_count": len(_score_log),
        "debug_scores_over_60": sum(
            1 for s in _score_log if s >= 60
        ),
        "debug_scores_over_70": sum(
            1 for s in _score_log if s >= 70
        ),
        "debug_scores_over_80": sum(
            1 for s in _score_log if s >= 80
        ),
        "debug_max_base_score": max(
            (row["base_score"] for row in _score_detail_log),
            default=None,
        ),
        "debug_max_adx": max(
            (row["adx"] for row in _score_detail_log),
            default=None,
        ),
        "debug_max_adx_bonus": max(
            (row["adx_bonus"] for row in _score_detail_log),
            default=None,
        ),
        "debug_max_volume_ratio": max(
            (row["volume_ratio"] for row in _score_detail_log),
            default=None,
        ),
        "debug_max_volume_bonus": max(
            (row["volume_bonus"] for row in _score_detail_log),
            default=None,
        ),
        "debug_max_mtf_bonus": max(
            (row["mtf_bonus"] for row in _score_detail_log),
            default=None,
        ),
        "debug_chase_block_count": sum(
            1
            for row in _score_detail_log
            if row["chase_blocked"]
        ),
        "debug_ema_chase_block_count": sum(
            1
            for row in _score_detail_log
            if row["ema_chase_blocked"]
        ),
        "debug_vwap_chase_block_count": sum(
            1
            for row in _score_detail_log
            if row["vwap_chase_blocked"]
        ),
        "debug_vwap_fallback_count": sum(
            1
            for row in _score_detail_log
            if row["vwap_fallback_used"]
        ),
        "debug_top_candidates": top_candidates,
        "exit_model": {
            "type": "DYNAMIC_ATR_PROFIT_LOCK",
            "fixed_percentage_sl": False,
            "fixed_target_enabled": False,
            "normal_option_response": 0.50,
            "expiry_option_response": 1.00,
            "normal_atr_multiplier": 1.20,
            "expiry_atr_multiplier": 1.50,
            "profit_lock_ladder": {
                "0.8R": "BREAKEVEN",
                "1.2R": "LOCK_0.5R",
                "1.8R": "LOCK_1R",
                "after_1.8R": "PEAK_MINUS_0.8R",
            },
            "trail_activation": "NEXT_CANDLE",
            "structural_reversal_exit": {
                "confirmation_candles": 2,
                "CE": (
                    "close<VWAP and close<EMA9 and "
                    "(ST DOWN or EMA9<EMA21)"
                ),
                "PE": (
                    "close>VWAP and close>EMA9 and "
                    "(ST UP or EMA9>EMA21)"
                ),
            },
            "entry_score_after_losses": 82,
            "loss_score_escalation_enabled": False,
            "reversal_confirmation_version": (
                "TRUE_OPPOSITE_V2"
            ),
            "reversal_requires": (
                "opposite ST+EMA trend flip OR "
                "valid opposite 82+ signal"
            ),
            "expiry_day": "TUESDAY",
            "ignored_request_fields": ["sl_percent","target_percent"],
        },
        "note": "Signal timing/score based on REAL historical index candles. Option premium is an ATR-based estimate since real historical option premiums aren't available from the broker's live scrip master.",
        "summary": {
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "capital": capital,
            "net_pnl": total_pnl,
            "note": "Real signal timing (historical candles). Premium P&L is an estimate.",
        }
    }


@router.post("/run")
def run_backtest(body: dict, authorization: str = Header(None)):
    try:
        user = get_current_user(authorization)

        conn = get_db()
        ensure_backtest_table(conn)

        body = body or {}
        instrument = body.get("instrument") or body.get("primary_instrument") or "NIFTY"
        run_date = body.get("date") or body.get("run_date") or datetime.utcnow().date().isoformat()
        capital = float(body.get("capital") or body.get("paper_capital") or 100000)
        entry_score = int(body.get("entry_score") or body.get("entry_threshold") or 82)
        sl_percent = float(body.get("sl_percent") or 12)
        target_percent = float(body.get("target_percent") or 24)
        strategy_mode = str(
            body.get("strategy_mode")
            or "NORMAL"
        ).upper()

        if is_weekend(run_date):
            conn.close()
            return {"success": False, "message": "Market holiday / weekend. No backtest for this date."}

        if not ENGINE_AVAILABLE:
            conn.close()
            return {"success": False, "message": "Backtest engine unavailable on server."}

        broker = conn.execute(
            "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
            (user["id"],)
        ).fetchone()

        if not broker:
            conn.close()
            return {"success": False, "message": "Broker connect karein backtest ke liye (real historical candles chahiye)."}

        broker_name = broker["broker_name"]
        try:
            creds = {
                "api_key": decrypt_credential(broker["api_key"]),
                "client_id": broker["client_id"],
                "password": decrypt_credential(broker["api_secret"]),
                "totp_secret": decrypt_credential(broker["totp_secret"]) if broker["totp_secret"] else None,
            }
            if broker_name == "angelone":
                obj = angel_login(creds)
            else:
                obj = create_broker(broker_name, creds["client_id"], creds["api_key"], creds["password"], creds.get("totp_secret"))
                login_result = obj.login()
                if not login_result.get("success"):
                    conn.close()
                    return {"success": False, "message": f"Broker login failed: {login_result.get('message','')[:150]}"}
        except Exception as e:
            conn.close()
            return {"success": False, "message": f"Broker login failed: {str(e)[:150]}"}

        raw_result = _okai_run_backtest_mode(
            broker_name=broker_name,
            obj=obj,
            instrument=instrument,
            date_str=run_date,
            capital=capital,
            entry_threshold=82,
            sl_percent=sl_percent,
            target_percent=target_percent,
            strategy_mode=strategy_mode,
        )

        json_stats = {"non_finite": 0}
        result = _json_safe(raw_result, json_stats)

        if isinstance(result, dict):
            result["debug_sanitized_non_finite"] = json_stats[
                "non_finite"
            ]

        # Validate strict JSON here so serialization errors become visible.
        json.dumps(result, allow_nan=False)

        if not result.get("success"):
            conn.close()
            return result

        conn.execute(
            """INSERT INTO backtest_runs
               (user_id, instrument, run_date, capital, entry_score, sl_percent, target_percent, result_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user["id"], instrument, run_date, capital, entry_score,
                sl_percent, target_percent,
                json.dumps(result, allow_nan=False),
                datetime.utcnow().isoformat()
            )
        )
        conn.commit()
        conn.close()

        try:
            msg = "\n".join([
                "📊 <b>Backtest Complete (Real Signal)</b>",
                f"Instrument: {instrument}",
                f"Strategy: {result.get('strategy_mode', strategy_mode)}",
                f"Date: {run_date}",
                f"Trades: {result['total_trades']}",
                f"Wins/Losses: {result['wins']}/{result['losses']}",
                f"Win Rate: {result['win_rate']}%",
                f"P&L: Rs {result['total_pnl']} (estimated premium)",
            ])
            notify_user(user["id"], msg)
        except Exception:
            pass

        return result

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": e.__class__.__name__,
            "message": "Backtest run failed, but error is now visible."
        }


    except Exception as e:
        import traceback
        return {"stage": "exception", "error": str(e), "trace": traceback.format_exc()[-1200:]}

@router.get("/history")
def backtest_history(authorization: str = Header(None)):
    try:
        user = get_current_user(authorization)

        conn = get_db()
        ensure_backtest_table(conn)

        rows = conn.execute(
            """SELECT * FROM backtest_runs
               WHERE user_id=?
               ORDER BY id DESC
               LIMIT 20""",
            (user["id"],)
        ).fetchall()

        history = []

        for r in rows:
            try:
                d = {k: r[k] for k in r.keys()}
            except Exception:
                d = {}

            try:
                result = json.loads(d.get("result_json") or "{}")
            except Exception:
                result = {}

            history.append({
                "id": d.get("id"),
                "instrument": d.get("instrument") or result.get("instrument"),
                "date": d.get("run_date") or result.get("date"),
                "capital": d.get("capital") or result.get("capital"),
                "entry_score": d.get("entry_score") or result.get("entry_score"),
                "total_trades": result.get("total_trades", 0),
                "wins": result.get("wins", 0),
                "losses": result.get("losses", 0),
                "win_rate": result.get("win_rate", 0),
                "total_pnl": result.get("total_pnl", 0),
                "summary": result.get("summary", {}),
                "created_at": d.get("created_at"),
                "result": result
            })

        conn.close()

        return {
            "success": True,
            "history": history,
            "backtests": history
        }

    except Exception as e:
        return {
            "success": False,
            "history": [],
            "backtests": [],
            "error": str(e),
            "error_type": e.__class__.__name__
        }


# ============================================================
# OKAI AUTO INDEX BACKTEST V1
# Scans NIFTY, BANKNIFTY and SENSEX.
# Keeps only one open trade at a time.
# Uses 90% of current equity with whole lots.
# ============================================================
from copy import deepcopy as _okai_deepcopy
from bot.position_sizing import (
    CAPITAL_USE_FRACTION as _OKAI_CAPITAL_USE_FRACTION,
    calculate_lot_sizing as _okai_calculate_lot_sizing,
)

_OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST = (
    run_realistic_day_backtest
)
_OKAI_AUTO_INSTRUMENTS = (
    "NIFTY",
    "BANKNIFTY",
    "SENSEX",
)
_OKAI_INSTRUMENT_PRIORITY = {
    "NIFTY": 0,
    "BANKNIFTY": 1,
    "SENSEX": 2,
}


def _okai_parse_trade_time(value):
    try:
        return datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        )
    except Exception:
        return datetime.min


def _okai_recalculate_summary(
    result,
    capital,
    trades,
):
    total_pnl = round(
        sum(
            float(trade.get("pnl") or 0)
            for trade in trades
        ),
        2,
    )
    wins = sum(
        1
        for trade in trades
        if float(trade.get("pnl") or 0) >= 0
    )
    losses = len(trades) - wins
    win_rate = (
        round(wins / len(trades) * 100, 2)
        if trades
        else 0
    )

    result["trades"] = trades
    result["total_trades"] = len(trades)
    result["wins"] = wins
    result["losses"] = losses
    result["win_rate"] = win_rate
    result["total_pnl"] = total_pnl
    result["ending_capital"] = round(
        float(capital) + total_pnl,
        2,
    )
    result["position_sizing"] = {
        "mode": "CAPITAL_90_PERCENT",
        "capital_use_percent": 90,
        "whole_lots_only": True,
        "equity_compounding": True,
        "one_open_trade_at_a_time": True,
    }

    summary = dict(
        result.get("summary") or {}
    )
    summary.update({
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "capital": float(capital),
        "net_pnl": total_pnl,
        "ending_capital": result[
            "ending_capital"
        ],
        "capital_use_percent": 90,
    })
    result["summary"] = summary
    return result


def _okai_scale_trades_to_capital(
    raw_result,
    capital,
):
    result = _okai_deepcopy(raw_result)
    if (
        not isinstance(result, dict)
        or not result.get("success")
    ):
        return result

    current_equity = float(capital)
    scaled = []

    for trade in result.get("trades", []):
        instrument = str(
            trade.get("instrument")
            or result.get("instrument")
            or "NIFTY"
        ).upper()
        lot_size = LOT_SIZES.get(
            instrument,
            1,
        )
        entry = float(
            trade.get("entry_price") or 0
        )
        exit_price = float(
            trade.get("exit_price") or 0
        )

        sizing = _okai_calculate_lot_sizing(
            current_equity,
            entry,
            lot_size,
            _OKAI_CAPITAL_USE_FRACTION,
        )

        if not sizing["affordable"]:
            continue

        updated = dict(trade)
        updated["trade_no"] = len(scaled) + 1
        updated["instrument"] = instrument
        updated["lot_size"] = lot_size
        updated["lots"] = sizing["lots"]
        updated["qty"] = sizing["quantity"]
        updated["capital_before_trade"] = round(
            current_equity,
            2,
        )
        updated["usable_capital"] = sizing[
            "usable_capital"
        ]
        updated["capital_used"] = sizing[
            "capital_used"
        ]
        updated[
            "capital_utilization_percent"
        ] = sizing[
            "capital_utilization_percent"
        ]
        updated["pnl"] = round(
            (exit_price - entry)
            * sizing["quantity"],
            2,
        )

        current_equity += updated["pnl"]
        updated["capital_after_trade"] = round(
            current_equity,
            2,
        )
        scaled.append(updated)

    return _okai_recalculate_summary(
        result,
        capital,
        scaled,
    )


def _okai_run_auto_index_backtest(
    broker_name,
    obj,
    date_str,
    capital,
    entry_threshold,
    sl_percent,
    target_percent,
):
    single_results = {}
    combined_trades = []
    combined_candidates = []

    for instrument_index, instrument in enumerate(
        _OKAI_AUTO_INSTRUMENTS
    ):
        if instrument_index > 0:
            # Avoid broker historical-candle burst limits.
            import time as _okai_time
            _okai_time.sleep(0.45)

        result = (
            _OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST(
                broker_name,
                obj,
                instrument,
                date_str,
                capital,
                entry_threshold,
                sl_percent,
                target_percent,
            )
        )
        single_results[instrument] = result

        if (
            not isinstance(result, dict)
            or not result.get("success")
        ):
            continue

        for trade in result.get("trades", []):
            row = dict(trade)
            row["instrument"] = instrument
            combined_trades.append(row)

        for candidate in result.get(
            "debug_top_candidates",
            [],
        ):
            row = dict(candidate)
            row["instrument"] = instrument
            combined_candidates.append(row)

    combined_trades.sort(
        key=lambda trade: (
            _okai_parse_trade_time(
                trade.get("entry_time")
            ),
            -int(trade.get("score") or 0),
            _OKAI_INSTRUMENT_PRIORITY.get(
                trade.get("instrument"),
                99,
            ),
        )
    )

    selected = []
    busy_until = None

    for trade in combined_trades:
        entry_time = _okai_parse_trade_time(
            trade.get("entry_time")
        )
        exit_time = _okai_parse_trade_time(
            trade.get("exit_time")
        )

        if (
            busy_until is not None
            and entry_time < busy_until
        ):
            continue

        selected.append(trade)
        busy_until = exit_time

    raw_auto = {
        "success": True,
        "instrument": "AUTO",
        "date": date_str,
        "capital": float(capital),
        "trades": selected,
        "debug_top_candidates": sorted(
            combined_candidates,
            key=lambda row: (
                int(row.get("score") or 0),
                -_OKAI_INSTRUMENT_PRIORITY.get(
                    row.get("instrument"),
                    99,
                ),
            ),
            reverse=True,
        )[:15],
        "debug_max_score": max(
            (
                result.get("debug_max_score")
                for result
                in single_results.values()
                if isinstance(result, dict)
                and result.get(
                    "debug_max_score"
                ) is not None
            ),
            default=None,
        ),
        "debug_chase_block_count": sum(
            int(
                result.get(
                    "debug_chase_block_count",
                    0,
                )
                or 0
            )
            for result
            in single_results.values()
            if isinstance(result, dict)
        ),
        "debug_ema_chase_block_count": sum(
            int(
                result.get(
                    "debug_ema_chase_block_count",
                    0,
                )
                or 0
            )
            for result
            in single_results.values()
            if isinstance(result, dict)
        ),
        "debug_vwap_chase_block_count": sum(
            int(
                result.get(
                    "debug_vwap_chase_block_count",
                    0,
                )
                or 0
            )
            for result
            in single_results.values()
            if isinstance(result, dict)
        ),
        "auto_scan": {
            "enabled": True,
            "instruments": list(
                _OKAI_AUTO_INSTRUMENTS
            ),
            "selection": (
                "EARLIEST_VALID_ENTRY_"
                "THEN_HIGHEST_SCORE"
            ),
            "simultaneous_trades": False,
            "model": (
                "CONSERVATIVE_MERGE_V1"
            ),
        },
        "per_instrument": {
            instrument: {
                "success": bool(
                    isinstance(result, dict)
                    and result.get("success")
                ),
                "message": (
                    result.get("message")
                    if isinstance(result, dict)
                    else None
                ),
                "trades": (
                    result.get(
                        "total_trades",
                        0,
                    )
                    if isinstance(result, dict)
                    else 0
                ),
                "one_lot_pnl": (
                    result.get(
                        "total_pnl",
                        0,
                    )
                    if isinstance(result, dict)
                    else 0
                ),
                "max_score": (
                    result.get(
                        "debug_max_score"
                    )
                    if isinstance(result, dict)
                    else None
                ),
            }
            for instrument, result
            in single_results.items()
        },
        "note": (
            "AUTO scans all three indices. "
            "Only one trade remains open at a time. "
            "Option premium remains an ATR estimate."
        ),
        "summary": {
            "capital": float(capital),
            "note": (
                "AUTO three-index scan with "
                "90% capital sizing."
            ),
        },
    }

    first_success = next(
        (
            result
            for result
            in single_results.values()
            if isinstance(result, dict)
            and result.get("success")
        ),
        None,
    )
    if first_success:
        raw_auto["exit_model"] = (
            first_success.get("exit_model")
        )

    return _okai_scale_trades_to_capital(
        raw_auto,
        capital,
    )


def run_realistic_day_backtest(
    broker_name,
    obj,
    instrument,
    date_str,
    capital,
    entry_threshold,
    sl_percent,
    target_percent,
):
    instrument = str(
        instrument or "AUTO"
    ).upper()

    if instrument == "AUTO":
        return _okai_run_auto_index_backtest(
            broker_name,
            obj,
            date_str,
            capital,
            entry_threshold,
            sl_percent,
            target_percent,
        )

    raw_result = (
        _OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST(
            broker_name,
            obj,
            instrument,
            date_str,
            capital,
            entry_threshold,
            sl_percent,
            target_percent,
        )
    )
    return _okai_scale_trades_to_capital(
        raw_result,
        capital,
    )


# ============================================================
# OKAI AUTO MONTHLY BACKTEST V1
# Runs every weekday in a YYYY-MM month, compounds equity
# day-to-day, skips holidays/no-data dates, and returns a
# compact day-wise report for the mobile app.
# ============================================================
def _okai_month_weekdays(month_text):
    import calendar

    year_text, month_number_text = str(
        month_text
    ).split("-", 1)
    year = int(year_text)
    month_number = int(month_number_text)

    if year < 2000 or year > 2100:
        raise ValueError("Invalid year")
    if month_number < 1 or month_number > 12:
        raise ValueError("Invalid month")

    last_day = calendar.monthrange(
        year,
        month_number,
    )[1]

    dates = []
    for day_number in range(1, last_day + 1):
        day = datetime(
            year,
            month_number,
            day_number,
        )
        if day.weekday() < 5:
            dates.append(
                day.strftime("%Y-%m-%d")
            )

    return dates


def _okai_month_drawdown(equity_curve):
    if not equity_curve:
        return {
            "max_drawdown": 0.0,
            "max_drawdown_percent": 0.0,
        }

    peak = float(equity_curve[0])
    max_drawdown = 0.0
    max_drawdown_percent = 0.0

    for value in equity_curve:
        equity = float(value)
        if equity > peak:
            peak = equity

        drawdown = max(0.0, peak - equity)
        drawdown_percent = (
            drawdown / peak * 100.0
            if peak > 0
            else 0.0
        )

        max_drawdown = max(
            max_drawdown,
            drawdown,
        )
        max_drawdown_percent = max(
            max_drawdown_percent,
            drawdown_percent,
        )

    return {
        "max_drawdown": round(
            max_drawdown,
            2,
        ),
        "max_drawdown_percent": round(
            max_drawdown_percent,
            2,
        ),
    }


def _okai_run_monthly_backtest_sync(
    body: dict,
    authorization: str = Header(None),
):
    import time as _okai_time

    try:
        user = get_current_user(authorization)
        body = body or {}

        month_text = str(
            body.get("month")
            or body.get("year_month")
            or datetime.utcnow().strftime("%Y-%m")
        ).strip()

        instrument = str(
            body.get("instrument")
            or "AUTO"
        ).upper()

        if instrument not in (
            "AUTO",
            "NIFTY",
            "BANKNIFTY",
            "SENSEX",
        ):
            instrument = "AUTO"

        starting_capital = float(
            body.get("capital")
            or body.get("paper_capital")
            or 100000
        )
        entry_score = 82
        sl_percent = 0.0
        target_percent = 0.0
        strategy_mode = str(
            body.get("strategy_mode")
            or "NORMAL"
        ).upper()

        all_month_dates = _okai_month_weekdays(
            month_text
        )

        ist_today = (
            datetime.now(timezone.utc)
            + timedelta(hours=5, minutes=30)
        ).date()

        # Use only completed historical dates.
        month_dates = [
            date_text
            for date_text in all_month_dates
            if datetime.strptime(
                date_text,
                "%Y-%m-%d",
            ).date() < ist_today
        ]

        if not month_dates:
            return {
                "success": False,
                "message": (
                    "Is month me abhi koi completed "
                    "trading date available nahi hai."
                ),
            }

        monthly_job_id = body.get(
            "_monthly_job_id"
        )

        conn = get_db()
        ensure_backtest_table(conn)

        broker = conn.execute(
            """SELECT *
               FROM broker_credentials
               WHERE user_id=?
                 AND is_active=1
               ORDER BY last_connected DESC
               LIMIT 1""",
            (user["id"],),
        ).fetchone()

        if not broker:
            conn.close()
            return {
                "success": False,
                "message": (
                    "Broker connect karein monthly "
                    "backtest ke liye."
                ),
            }

        broker_name = broker["broker_name"]

        try:
            creds = {
                "api_key": decrypt_credential(
                    broker["api_key"]
                ),
                "client_id": broker["client_id"],
                "password": decrypt_credential(
                    broker["api_secret"]
                ),
                "totp_secret": (
                    decrypt_credential(
                        broker["totp_secret"]
                    )
                    if broker["totp_secret"]
                    else None
                ),
            }

            if broker_name == "angelone":
                obj = angel_login(creds)
            else:
                obj = create_broker(
                    broker_name,
                    creds["client_id"],
                    creds["api_key"],
                    creds["password"],
                    creds.get("totp_secret"),
                )
                login_result = obj.login()

                if not login_result.get(
                    "success"
                ):
                    conn.close()
                    return {
                        "success": False,
                        "message": (
                            "Broker login failed: "
                            + str(
                                login_result.get(
                                    "message",
                                    "",
                                )
                            )[:150]
                        ),
                    }

        except Exception as exc:
            conn.close()
            return {
                "success": False,
                "message": (
                    "Broker login failed: "
                    + str(exc)[:150]
                ),
            }

        current_capital = starting_capital
        day_results = []
        all_trades = []
        equity_curve = [starting_capital]

        total_trades = 0
        total_wins = 0
        total_losses = 0
        winning_days = 0
        losing_days = 0
        flat_days = 0
        tested_days = 0
        skipped_days = 0
        total_normal_pnl = 0.0
        total_hero_zero_pnl = 0.0

        for index, date_text in enumerate(
            month_dates
        ):
            _okai_update_monthly_job(
                monthly_job_id,
                completed_days=index,
                total_days=len(month_dates),
                current_date=date_text,
                phase="RUNNING",
            )

            if index > 0:
                # One AUTO day may make three historical
                # candle requests, so keep a safe gap.
                _okai_time.sleep(0.75)

            raw_day = _okai_run_backtest_mode(
                broker_name=broker_name,
                obj=obj,
                instrument=instrument,
                date_str=date_text,
                capital=current_capital,
                entry_threshold=82,
                sl_percent=sl_percent,
                target_percent=target_percent,
                strategy_mode=strategy_mode,
            )

            json_stats = {"non_finite": 0}
            day = _json_safe(
                raw_day,
                json_stats,
            )

            if (
                not isinstance(day, dict)
                or not day.get("success")
            ):
                skipped_days += 1
                day_results.append({
                    "date": date_text,
                    "status": "SKIPPED",
                    "message": (
                        day.get("message")
                        if isinstance(day, dict)
                        else "Invalid result"
                    ),
                    "capital_start": round(
                        current_capital,
                        2,
                    ),
                    "capital_end": round(
                        current_capital,
                        2,
                    ),
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "pnl": 0.0,
                })
                continue

            tested_days += 1

            day_pnl = float(
                day.get("total_pnl") or 0
            )
            day_trades = int(
                day.get("total_trades") or 0
            )
            day_wins = int(
                day.get("wins") or 0
            )
            day_losses = int(
                day.get("losses") or 0
            )
            ending_capital = float(
                day.get("ending_capital")
                or current_capital + day_pnl
            )

            if day_pnl > 0:
                winning_days += 1
                day_status = "PROFIT"
            elif day_pnl < 0:
                losing_days += 1
                day_status = "LOSS"
            else:
                flat_days += 1
                day_status = "FLAT"

            per_instrument = (
                day.get("per_instrument")
                if isinstance(
                    day.get("per_instrument"),
                    dict,
                )
                else {}
            )

            day_results.append({
                "date": date_text,
                "status": day_status,
                "capital_start": round(
                    current_capital,
                    2,
                ),
                "capital_end": round(
                    ending_capital,
                    2,
                ),
                "trades": day_trades,
                "wins": day_wins,
                "losses": day_losses,
                "win_rate": float(
                    day.get("win_rate") or 0
                ),
                "pnl": round(day_pnl, 2),
                "strategy_mode": day.get(
                    "strategy_mode",
                    strategy_mode,
                ),
                "normal_pnl": round(
                    float(day.get("normal_pnl") or 0),
                    2,
                ),
                "hero_zero_pnl": round(
                    float(day.get("hero_zero_pnl") or 0),
                    2,
                ),
                "max_score": day.get(
                    "debug_max_score"
                ),
                "per_instrument": (
                    per_instrument
                ),
            })

            for trade in day.get(
                "trades",
                [],
            ):
                trade_row = dict(trade)
                trade_row["date"] = date_text
                all_trades.append(trade_row)

            current_capital = ending_capital
            equity_curve.append(
                current_capital
            )

            total_trades += day_trades
            total_wins += day_wins
            total_losses += day_losses
            total_normal_pnl += float(
                day.get("normal_pnl") or 0
            )
            total_hero_zero_pnl += float(
                day.get("hero_zero_pnl") or 0
            )

            _okai_update_monthly_job(
                monthly_job_id,
                completed_days=index + 1,
                total_days=len(month_dates),
                current_date=date_text,
                phase="RUNNING",
            )

        net_pnl = round(
            current_capital
            - starting_capital,
            2,
        )
        win_rate = (
            round(
                total_wins
                / total_trades
                * 100,
                2,
            )
            if total_trades
            else 0
        )
        drawdown = _okai_month_drawdown(
            equity_curve
        )

        result = {
            "success": True,
            "period": "MONTHLY",
            "month": month_text,
            "instrument": instrument,
            "strategy_mode": strategy_mode,
            "normal_pnl": round(
                total_normal_pnl,
                2,
            ),
            "hero_zero_pnl": round(
                total_hero_zero_pnl,
                2,
            ),
            "capital": round(
                starting_capital,
                2,
            ),
            "ending_capital": round(
                current_capital,
                2,
            ),
            "total_pnl": net_pnl,
            "total_trades": total_trades,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": win_rate,
            "trading_weekdays": len(
                month_dates
            ),
            "tested_days": tested_days,
            "skipped_days": skipped_days,
            "winning_days": winning_days,
            "losing_days": losing_days,
            "flat_days": flat_days,
            "days": day_results,
            "trades": all_trades,
            "equity_curve": [
                round(value, 2)
                for value in equity_curve
            ],
            "max_drawdown": drawdown[
                "max_drawdown"
            ],
            "max_drawdown_percent": drawdown[
                "max_drawdown_percent"
            ],
            "position_sizing": {
                "mode": (
                    "CAPITAL_90_PERCENT"
                ),
                "capital_use_percent": 90,
                "equity_compounding": True,
                "whole_lots_only": True,
            },
            "auto_scan": {
                "enabled": (
                    instrument == "AUTO"
                ),
                "instruments": (
                    list(
                        _OKAI_AUTO_INSTRUMENTS
                    )
                    if instrument == "AUTO"
                    else [instrument]
                ),
                "one_open_trade_at_a_time": True,
            },
            "summary": {
                "period": "MONTHLY",
                "month": month_text,
                "instrument": instrument,
                "strategy_mode": strategy_mode,
                "normal_pnl": round(
                    total_normal_pnl,
                    2,
                ),
                "hero_zero_pnl": round(
                    total_hero_zero_pnl,
                    2,
                ),
                "trades": total_trades,
                "wins": total_wins,
                "losses": total_losses,
                "win_rate": win_rate,
                "capital": round(
                    starting_capital,
                    2,
                ),
                "ending_capital": round(
                    current_capital,
                    2,
                ),
                "net_pnl": net_pnl,
                "tested_days": tested_days,
                "skipped_days": skipped_days,
                "winning_days": winning_days,
                "losing_days": losing_days,
                "flat_days": flat_days,
                "max_drawdown": drawdown[
                    "max_drawdown"
                ],
                "max_drawdown_percent": (
                    drawdown[
                        "max_drawdown_percent"
                    ]
                ),
                "capital_use_percent": 90,
                "note": (
                    "Monthly AUTO backtest uses "
                    "90% current equity and "
                    "ATR-estimated option premiums."
                ),
            },
        }

        safe_result = _json_safe(result)
        json.dumps(
            safe_result,
            allow_nan=False,
        )

        conn.execute(
            """INSERT INTO backtest_runs
               (
                 user_id,
                 instrument,
                 run_date,
                 capital,
                 entry_score,
                 sl_percent,
                 target_percent,
                 result_json,
                 created_at
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user["id"],
                (
                    instrument
                    + "_"
                    + strategy_mode
                    + "_MONTHLY"
                ),
                month_text,
                starting_capital,
                entry_score,
                sl_percent,
                target_percent,
                json.dumps(
                    safe_result,
                    allow_nan=False,
                ),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        try:
            notify_user(
                user["id"],
                "\n".join([
                    "📅 <b>Monthly Backtest Complete</b>",
                    f"Month: {month_text}",
                    f"Instrument: {instrument}",
                    f"Strategy: {strategy_mode}",
                    f"Tested Days: {tested_days}",
                    f"Trades: {total_trades}",
                    (
                        f"Wins/Losses: "
                        f"{total_wins}/{total_losses}"
                    ),
                    f"Net P&L: Rs {net_pnl}",
                    (
                        f"Ending Capital: "
                        f"Rs {round(current_capital, 2)}"
                    ),
                ]),
            )
        except Exception:
            pass

        return safe_result

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": (
                exc.__class__.__name__
            ),
            "message": (
                "Monthly backtest failed, "
                "but error is visible."
            ),
        }


# ============================================================
# OKAI HERO ZERO BACKTEST V1
# Tuesday expiry, entry 14:30-15:00 IST, force exit 15:25,
# score 82, estimated premium Rs 0.50-Rs 10,
# capital cap Rs 2,000, max one trade per day.
# ============================================================
_OKAI_HERO_WINDOW_START = 14 * 60 + 30
_OKAI_HERO_WINDOW_END = 15 * 60
_OKAI_HERO_FORCE_EXIT = 15 * 60 + 25
_OKAI_HERO_CAPITAL_CAP = 2000.0
_OKAI_HERO_PREMIUM_MIN = 0.50
_OKAI_HERO_PREMIUM_MAX = 10.00
_OKAI_HERO_GAMMA_RESPONSE = 25.0
_OKAI_HERO_THETA_DECAY_PER_MINUTE = 0.006
_OKAI_HERO_SL_FRACTION = 0.50
_OKAI_HERO_TARGET_MULTIPLE = 2.00


def _okai_normalize_strategy_mode(value):
    text = str(value or "NORMAL").upper()
    aliases = {
        "HERO": "HERO_ZERO",
        "HEROZERO": "HERO_ZERO",
        "HERO ZERO": "HERO_ZERO",
        "BOTH": "COMBINED",
        "NORMAL+HERO": "COMBINED",
        "NORMAL_HERO": "COMBINED",
    }
    text = aliases.get(text, text)
    if text not in (
        "NORMAL",
        "HERO_ZERO",
        "COMBINED",
    ):
        text = "NORMAL"
    return text


def _okai_is_tuesday_expiry(date_str):
    try:
        return (
            datetime.fromisoformat(
                str(date_str)
            ).weekday()
            == 1
        )
    except Exception:
        return False


def _okai_hero_estimated_entry_premium(
    spot_atr,
    candle_minutes,
):
    try:
        atr = max(
            0.0,
            float(spot_atr or 0),
        )
    except (TypeError, ValueError):
        atr = 0.0

    elapsed = max(
        0,
        min(
            30,
            int(candle_minutes)
            - _OKAI_HERO_WINDOW_START,
        ),
    )
    window_decay = max(
        0.55,
        1.0 - elapsed / 70.0,
    )
    premium = atr * 0.22 * window_decay
    premium = max(
        _OKAI_HERO_PREMIUM_MIN,
        min(
            _OKAI_HERO_PREMIUM_MAX,
            premium,
        ),
    )
    premium = round(premium * 2.0) / 2.0
    return round(
        max(
            _OKAI_HERO_PREMIUM_MIN,
            premium,
        ),
        2,
    )


def _okai_hero_sizing(
    capital,
    entry_price,
    lot_size,
):
    try:
        available_capital = max(
            0.0,
            min(
                float(capital or 0),
                _OKAI_HERO_CAPITAL_CAP,
            ),
        )
        entry = max(
            0.0,
            float(entry_price or 0),
        )
        lot = max(
            1,
            int(lot_size or 1),
        )
    except (TypeError, ValueError):
        available_capital = 0.0
        entry = 0.0
        lot = 1

    one_lot_cost = entry * lot
    lots = (
        int(available_capital // one_lot_cost)
        if one_lot_cost > 0
        else 0
    )
    qty = lots * lot
    capital_used = qty * entry

    return {
        "hero_capital_cap": round(
            available_capital,
            2,
        ),
        "lot_size": lot,
        "lots": lots,
        "quantity": qty,
        "capital_used": round(
            capital_used,
            2,
        ),
        "hero_capital_utilization_percent": (
            round(
                capital_used
                / available_capital
                * 100,
                2,
            )
            if available_capital > 0
            else 0.0
        ),
        "affordable": lots >= 1,
    }


def _okai_empty_hero_result(
    instrument,
    date_str,
    capital,
    reason,
    expiry_day,
):
    return {
        "success": True,
        "period": "DAILY",
        "instrument": instrument,
        "date": date_str,
        "strategy_mode": "HERO_ZERO",
        "expiry_day": bool(expiry_day),
        "hero_zero_eligible": bool(expiry_day),
        "hero_zero_reason": reason,
        "capital": round(float(capital), 2),
        "ending_capital": round(
            float(capital),
            2,
        ),
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
        "total_pnl": 0.0,
        "normal_pnl": 0.0,
        "hero_zero_pnl": 0.0,
        "trades": [],
        "hero_zero": {
            "entry_window": "14:30-15:00 IST",
            "force_exit": "15:25 IST",
            "expiry_day": "TUESDAY",
            "capital_cap": _OKAI_HERO_CAPITAL_CAP,
            "premium_range": [
                _OKAI_HERO_PREMIUM_MIN,
                _OKAI_HERO_PREMIUM_MAX,
            ],
            "entry_score": 82,
            "max_trades_per_day": 1,
            "premium_model": (
                "EXPIRY_GAMMA_ESTIMATE_V1"
            ),
        },
        "summary": {
            "period": "DAILY",
            "strategy_mode": "HERO_ZERO",
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "capital": round(
                float(capital),
                2,
            ),
            "ending_capital": round(
                float(capital),
                2,
            ),
            "net_pnl": 0.0,
            "normal_pnl": 0.0,
            "hero_zero_pnl": 0.0,
            "note": reason,
        },
    }


def _okai_run_hero_zero_single(
    broker_name,
    obj,
    instrument,
    date_str,
    capital,
):
    expiry_day = _okai_is_tuesday_expiry(
        date_str
    )

    if not expiry_day:
        return _okai_empty_hero_result(
            instrument,
            date_str,
            capital,
            "Hero Zero skipped: Tuesday expiry day nahi hai.",
            False,
        )

    try:
        df = fetch_backtest_candles(
            broker_name,
            obj,
            instrument,
            date_str,
        )
    except Exception as exc:
        return {
            "success": False,
            "message": (
                "Historical data fetch failed: "
                + str(exc)[:150]
            ),
        }

    if df is None or df.empty:
        return {
            "success": False,
            "message": (
                "No data found for this date "
                "(market holiday or no historical data)."
            ),
        }

    if len(df) < 30:
        return {
            "success": False,
            "message": (
                "Insufficient candle data for this date."
            ),
        }

    full_indicator_df = _okai_precompute_indicators(
        df
    )
    if full_indicator_df is None:
        return {
            "success": False,
            "message": (
                "Unable to calculate Hero Zero "
                "indicators for this date."
            ),
        }

    open_trade = None
    closed_trade = None
    score_log = []
    candidate_log = []

    for index in range(
        28,
        len(full_indicator_df),
    ):
        wdf = full_indicator_df.iloc[
            :index + 1
        ]
        last = wdf.iloc[-1]
        trend = _okai_trend_from_indicator_row(
            last
        )
        c1 = (
            wdf.iloc[-3]
            if len(wdf) >= 3
            else wdf.iloc[-1]
        )
        c2 = (
            wdf.iloc[-2]
            if len(wdf) >= 2
            else wdf.iloc[-1]
        )

        candle_minutes = _candle_minutes_ist(
            last["time"]
        )
        price = float(last["close"])

        if open_trade:
            side = open_trade["side"]
            entry = open_trade["entry_price"]
            entry_spot = open_trade[
                "entry_spot"
            ]
            entry_minutes = open_trade[
                "entry_minutes"
            ]

            spot_close = float(last["close"])
            spot_high = float(last["high"])
            spot_low = float(last["low"])

            close_pct = (
                spot_close - entry_spot
            ) / entry_spot * 100

            if side == "CE":
                good_pct = (
                    spot_high - entry_spot
                ) / entry_spot * 100
                bad_pct = (
                    spot_low - entry_spot
                ) / entry_spot * 100
            else:
                close_pct = -close_pct
                good_pct = (
                    entry_spot - spot_low
                ) / entry_spot * 100
                bad_pct = (
                    entry_spot - spot_high
                ) / entry_spot * 100

            elapsed_minutes = max(
                0,
                candle_minutes - entry_minutes,
            )
            theta_factor = max(
                0.25,
                1.0
                - elapsed_minutes
                * _OKAI_HERO_THETA_DECAY_PER_MINUTE,
            )

            current_premium = max(
                0.05,
                entry
                * (
                    1.0
                    + close_pct
                    * _OKAI_HERO_GAMMA_RESPONSE
                    / 100.0
                )
                * theta_factor,
            )
            premium_high = max(
                0.05,
                entry
                * (
                    1.0
                    + good_pct
                    * _OKAI_HERO_GAMMA_RESPONSE
                    / 100.0
                )
                * theta_factor,
            )
            premium_low = max(
                0.05,
                entry
                * (
                    1.0
                    + bad_pct
                    * _OKAI_HERO_GAMMA_RESPONSE
                    / 100.0
                )
                * theta_factor,
            )

            sl_price = open_trade[
                "sl_price"
            ]
            target_price = open_trade[
                "target_price"
            ]

            hit_sl = premium_low <= sl_price
            hit_target = (
                premium_high >= target_price
            )
            force_exit = (
                candle_minutes
                >= _OKAI_HERO_FORCE_EXIT
            )
            is_last = index == len(df) - 1

            if (
                hit_sl
                or hit_target
                or force_exit
                or is_last
            ):
                if hit_sl:
                    exit_price = round(
                        sl_price,
                        2,
                    )
                    exit_reason = (
                        "HERO_ZERO_50_PERCENT_SL"
                    )
                elif hit_target:
                    exit_price = round(
                        target_price,
                        2,
                    )
                    exit_reason = (
                        "HERO_ZERO_100_PERCENT_TARGET"
                    )
                else:
                    exit_price = round(
                        current_premium,
                        2,
                    )
                    exit_reason = (
                        "HERO_ZERO_FORCE_EXIT_1525"
                        if force_exit
                        else "HERO_ZERO_DAY_END"
                    )

                qty = open_trade["qty"]
                pnl = round(
                    (exit_price - entry) * qty,
                    2,
                )

                closed_trade = {
                    "trade_no": 1,
                    "strategy": "HERO_ZERO",
                    "instrument": instrument,
                    "symbol": (
                        f"{instrument} HEROZERO {side}"
                    ),
                    "side": side,
                    "score": open_trade["score"],
                    "entry_time": open_trade[
                        "entry_time"
                    ],
                    "exit_time": str(
                        last["time"]
                    ),
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "sl_price": sl_price,
                    "target_price": target_price,
                    "pnl": pnl,
                    "reason": exit_reason,
                    "lot_size": open_trade[
                        "lot_size"
                    ],
                    "lots": open_trade["lots"],
                    "qty": qty,
                    "capital_before_trade": round(
                        float(capital),
                        2,
                    ),
                    "capital_used": open_trade[
                        "capital_used"
                    ],
                    "capital_utilization_percent": (
                        round(
                            open_trade[
                                "capital_used"
                            ]
                            / float(capital)
                            * 100,
                            2,
                        )
                        if float(capital) > 0
                        else 0.0
                    ),
                    "hero_capital_cap": open_trade[
                        "hero_capital_cap"
                    ],
                    "hero_capital_utilization_percent": (
                        open_trade[
                            "hero_capital_utilization_percent"
                        ]
                    ),
                    "capital_after_trade": round(
                        float(capital) + pnl,
                        2,
                    ),
                    "entry_spot": entry_spot,
                    "spot_atr_at_entry": open_trade[
                        "spot_atr_at_entry"
                    ],
                    "estimated_premium_high": round(
                        premium_high,
                        2,
                    ),
                    "estimated_premium_low": round(
                        premium_low,
                        2,
                    ),
                    "gamma_response_factor": (
                        _OKAI_HERO_GAMMA_RESPONSE
                    ),
                    "theta_decay_per_minute": (
                        _OKAI_HERO_THETA_DECAY_PER_MINUTE
                    ),
                    "expiry_day": True,
                    "fixed_target_enabled": True,
                }
                break

            continue

        if not (
            _OKAI_HERO_WINDOW_START
            <= candle_minutes
            < _OKAI_HERO_WINDOW_END
        ):
            continue

        orb_high, orb_low = calculate_orb_levels(
            wdf
        )
        market_data = {
            "price": price,
            "vwap": float(last["VWAP"]),
            "ema9": float(last["EMA9"]),
            "ema21": float(last["EMA21"]),
            "adx": float(last["ADX"]),
            "volume_ratio": float(
                last["VOL_RATIO"]
            ),
            "vwap_fallback_used": bool(
                last["VWAP_FALLBACK_USED"]
            ),
            "supertrend_dir": str(
                last["ST_DIR"]
            ),
            "trend": trend,
            "mtf_confirmed": (
                trend != "SIDEWAYS"
            ),
            "c1_bullish": (
                float(c1["close"])
                > float(c1["open"])
            ),
            "c2_bullish": (
                float(c2["close"])
                > float(c2["open"])
            ),
            "gap_day": False,
            "orb_high": orb_high,
            "orb_low": orb_low,
            "atr": float(last["ATR"]),
        }

        signal_data = get_full_signal(
            market_data,
            consecutive_losses=0,
        )
        score = int(
            signal_data.get("score", 0)
            or 0
        )
        score_log.append(score)

        candidate_log.append({
            "time": str(last["time"]),
            "instrument": instrument,
            "candidate_signal": (
                signal_data.get(
                    "candidate_signal"
                )
            ),
            "final_signal": (
                signal_data.get("signal")
            ),
            "score": score,
            "trade_allowed": bool(
                signal_data.get(
                    "trade_allowed",
                    False,
                )
            ),
            "ema_stretch_points": (
                signal_data.get(
                    "ema_stretch_points",
                    0,
                )
            ),
            "ema_stretch_limit": (
                signal_data.get(
                    "ema_stretch_limit",
                    0,
                )
            ),
            "warnings": signal_data.get(
                "warnings",
                [],
            ),
        })

        if not (
            signal_data.get("trade_allowed")
            and signal_data.get("signal")
            in ("CE", "PE")
            and score >= 82
        ):
            continue

        entry_premium = (
            _okai_hero_estimated_entry_premium(
                market_data["atr"],
                candle_minutes,
            )
        )
        sizing = _okai_hero_sizing(
            capital,
            entry_premium,
            LOT_SIZES.get(instrument, 1),
        )

        if not sizing["affordable"]:
            continue

        open_trade = {
            "side": signal_data["signal"],
            "score": score,
            "entry_time": str(last["time"]),
            "entry_minutes": candle_minutes,
            "entry_spot": price,
            "entry_price": entry_premium,
            "sl_price": round(
                entry_premium
                * _OKAI_HERO_SL_FRACTION,
                2,
            ),
            "target_price": round(
                entry_premium
                * _OKAI_HERO_TARGET_MULTIPLE,
                2,
            ),
            "spot_atr_at_entry": (
                market_data["atr"]
            ),
            **sizing,
            "qty": sizing["quantity"],
        }

    if closed_trade is None:
        reason = (
            "Hero Zero: 14:30-15:00 me "
            "valid score 82 setup nahi mila."
        )
        result = _okai_empty_hero_result(
            instrument,
            date_str,
            capital,
            reason,
            True,
        )
        result["debug_max_score"] = (
            max(score_log)
            if score_log
            else None
        )
        result["debug_top_candidates"] = sorted(
            candidate_log,
            key=lambda row: row["score"],
            reverse=True,
        )[:10]
        return result

    pnl = float(closed_trade["pnl"])
    wins = 1 if pnl >= 0 else 0
    losses = 1 - wins

    return {
        "success": True,
        "period": "DAILY",
        "instrument": instrument,
        "date": date_str,
        "strategy_mode": "HERO_ZERO",
        "expiry_day": True,
        "hero_zero_eligible": True,
        "capital": round(
            float(capital),
            2,
        ),
        "ending_capital": round(
            float(capital) + pnl,
            2,
        ),
        "total_trades": 1,
        "wins": wins,
        "losses": losses,
        "win_rate": (
            100.0 if wins else 0.0
        ),
        "total_pnl": round(pnl, 2),
        "normal_pnl": 0.0,
        "hero_zero_pnl": round(pnl, 2),
        "trades": [closed_trade],
        "debug_max_score": (
            max(score_log)
            if score_log
            else closed_trade["score"]
        ),
        "debug_top_candidates": sorted(
            candidate_log,
            key=lambda row: row["score"],
            reverse=True,
        )[:10],
        "hero_zero": {
            "entry_window": "14:30-15:00 IST",
            "force_exit": "15:25 IST",
            "expiry_day": "TUESDAY",
            "capital_cap": _OKAI_HERO_CAPITAL_CAP,
            "premium_range": [
                _OKAI_HERO_PREMIUM_MIN,
                _OKAI_HERO_PREMIUM_MAX,
            ],
            "entry_score": 82,
            "max_trades_per_day": 1,
            "sl_percent": 50,
            "target_percent": 100,
            "premium_model": (
                "EXPIRY_GAMMA_ESTIMATE_V1"
            ),
        },
        "summary": {
            "period": "DAILY",
            "strategy_mode": "HERO_ZERO",
            "trades": 1,
            "wins": wins,
            "losses": losses,
            "win_rate": (
                100.0 if wins else 0.0
            ),
            "capital": round(
                float(capital),
                2,
            ),
            "ending_capital": round(
                float(capital) + pnl,
                2,
            ),
            "net_pnl": round(pnl, 2),
            "normal_pnl": 0.0,
            "hero_zero_pnl": round(
                pnl,
                2,
            ),
            "note": (
                "Hero Zero uses maximum Rs 2,000 "
                "capital and estimated expiry gamma premium."
            ),
        },
    }


def _okai_run_hero_zero_day(
    broker_name,
    obj,
    instrument,
    date_str,
    capital,
):
    instrument = str(
        instrument or "AUTO"
    ).upper()

    if instrument != "AUTO":
        return _okai_run_hero_zero_single(
            broker_name,
            obj,
            instrument,
            date_str,
            capital,
        )

    if not _okai_is_tuesday_expiry(
        date_str
    ):
        return _okai_empty_hero_result(
            "AUTO",
            date_str,
            capital,
            "Hero Zero skipped: Tuesday expiry day nahi hai.",
            False,
        )

    import time as _okai_hero_time

    per_instrument = {}
    candidates = []

    for position, symbol in enumerate(
        _OKAI_AUTO_INSTRUMENTS
    ):
        if position > 0:
            _okai_hero_time.sleep(0.45)

        result = _okai_run_hero_zero_single(
            broker_name,
            obj,
            symbol,
            date_str,
            capital,
        )
        per_instrument[symbol] = result

        if (
            isinstance(result, dict)
            and result.get("success")
            and result.get("trades")
        ):
            candidates.append(
                dict(result["trades"][0])
            )

    if not candidates:
        result = _okai_empty_hero_result(
            "AUTO",
            date_str,
            capital,
            (
                "Hero Zero AUTO: tino indices me "
                "valid score 82 setup nahi mila."
            ),
            True,
        )
        result["per_instrument"] = {
            symbol: {
                "trades": int(
                    value.get(
                        "total_trades",
                        0,
                    )
                    or 0
                ),
                "pnl": float(
                    value.get(
                        "total_pnl",
                        0,
                    )
                    or 0
                ),
                "max_score": value.get(
                    "debug_max_score"
                ),
                "reason": value.get(
                    "hero_zero_reason"
                ),
            }
            for symbol, value
            in per_instrument.items()
            if isinstance(value, dict)
        }
        result["debug_max_score"] = max(
            (
                value.get("debug_max_score")
                for value
                in per_instrument.values()
                if isinstance(value, dict)
                and value.get(
                    "debug_max_score"
                ) is not None
            ),
            default=None,
        )
        return result

    candidates.sort(
        key=lambda trade: (
            _okai_parse_trade_time(
                trade.get("entry_time")
            ),
            -int(trade.get("score") or 0),
            _OKAI_INSTRUMENT_PRIORITY.get(
                trade.get("instrument"),
                99,
            ),
        )
    )
    chosen = candidates[0]
    pnl = float(chosen.get("pnl") or 0)
    wins = 1 if pnl >= 0 else 0
    losses = 1 - wins

    return {
        "success": True,
        "period": "DAILY",
        "instrument": "AUTO",
        "selected_instrument": chosen.get(
            "instrument"
        ),
        "date": date_str,
        "strategy_mode": "HERO_ZERO",
        "expiry_day": True,
        "hero_zero_eligible": True,
        "capital": round(
            float(capital),
            2,
        ),
        "ending_capital": round(
            float(capital) + pnl,
            2,
        ),
        "total_trades": 1,
        "wins": wins,
        "losses": losses,
        "win_rate": (
            100.0 if wins else 0.0
        ),
        "total_pnl": round(pnl, 2),
        "normal_pnl": 0.0,
        "hero_zero_pnl": round(pnl, 2),
        "trades": [chosen],
        "debug_max_score": max(
            int(trade.get("score") or 0)
            for trade in candidates
        ),
        "per_instrument": {
            symbol: {
                "trades": int(
                    value.get(
                        "total_trades",
                        0,
                    )
                    or 0
                ),
                "pnl": float(
                    value.get(
                        "total_pnl",
                        0,
                    )
                    or 0
                ),
                "max_score": value.get(
                    "debug_max_score"
                ),
                "reason": value.get(
                    "hero_zero_reason"
                ),
            }
            for symbol, value
            in per_instrument.items()
            if isinstance(value, dict)
        },
        "hero_zero": {
            "entry_window": "14:30-15:00 IST",
            "force_exit": "15:25 IST",
            "expiry_day": "TUESDAY",
            "capital_cap": _OKAI_HERO_CAPITAL_CAP,
            "premium_range": [
                _OKAI_HERO_PREMIUM_MIN,
                _OKAI_HERO_PREMIUM_MAX,
            ],
            "entry_score": 82,
            "max_trades_per_day": 1,
            "sl_percent": 50,
            "target_percent": 100,
            "premium_model": (
                "EXPIRY_GAMMA_ESTIMATE_V1"
            ),
        },
        "summary": {
            "period": "DAILY",
            "strategy_mode": "HERO_ZERO",
            "trades": 1,
            "wins": wins,
            "losses": losses,
            "win_rate": (
                100.0 if wins else 0.0
            ),
            "capital": round(
                float(capital),
                2,
            ),
            "ending_capital": round(
                float(capital) + pnl,
                2,
            ),
            "net_pnl": round(pnl, 2),
            "normal_pnl": 0.0,
            "hero_zero_pnl": round(
                pnl,
                2,
            ),
            "note": (
                "AUTO Hero Zero selected the earliest "
                "valid 82+ setup across all three indices."
            ),
        },
    }


def _okai_combine_normal_and_hero(
    normal_result,
    hero_result,
    capital,
    instrument,
    date_str,
):
    if not (
        isinstance(normal_result, dict)
        and normal_result.get("success")
    ):
        return normal_result

    if not (
        isinstance(hero_result, dict)
        and hero_result.get("success")
    ):
        return normal_result

    combined_candidates = []

    for trade in normal_result.get(
        "trades",
        [],
    ):
        row = dict(trade)
        row["strategy"] = "NORMAL"
        combined_candidates.append(row)

    for trade in hero_result.get(
        "trades",
        [],
    ):
        row = dict(trade)
        row["strategy"] = "HERO_ZERO"
        combined_candidates.append(row)

    strategy_priority = {
        "HERO_ZERO": 0,
        "NORMAL": 1,
    }

    combined_candidates.sort(
        key=lambda trade: (
            _okai_parse_trade_time(
                trade.get("entry_time")
            ),
            -int(trade.get("score") or 0),
            strategy_priority.get(
                trade.get("strategy"),
                99,
            ),
        )
    )

    selected = []
    busy_until = None

    for trade in combined_candidates:
        entry_time = _okai_parse_trade_time(
            trade.get("entry_time")
        )
        exit_time = _okai_parse_trade_time(
            trade.get("exit_time")
        )

        if (
            busy_until is not None
            and entry_time < busy_until
        ):
            continue

        selected.append(trade)
        busy_until = exit_time

    normal_pnl = round(
        sum(
            float(trade.get("pnl") or 0)
            for trade in selected
            if trade.get("strategy")
            == "NORMAL"
        ),
        2,
    )
    hero_pnl = round(
        sum(
            float(trade.get("pnl") or 0)
            for trade in selected
            if trade.get("strategy")
            == "HERO_ZERO"
        ),
        2,
    )
    total_pnl = round(
        normal_pnl + hero_pnl,
        2,
    )
    wins = sum(
        1
        for trade in selected
        if float(trade.get("pnl") or 0) >= 0
    )
    losses = len(selected) - wins
    win_rate = (
        round(
            wins / len(selected) * 100,
            2,
        )
        if selected
        else 0
    )

    for number, trade in enumerate(
        selected,
        start=1,
    ):
        trade["trade_no"] = number

    scores = [
        value
        for value in (
            normal_result.get("debug_max_score"),
            hero_result.get("debug_max_score"),
        )
        if value is not None
    ]

    return {
        "success": True,
        "period": "DAILY",
        "instrument": instrument,
        "date": date_str,
        "strategy_mode": "COMBINED",
        "capital": round(
            float(capital),
            2,
        ),
        "ending_capital": round(
            float(capital) + total_pnl,
            2,
        ),
        "total_trades": len(selected),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "normal_pnl": normal_pnl,
        "hero_zero_pnl": hero_pnl,
        "trades": selected,
        "debug_max_score": (
            max(scores)
            if scores
            else None
        ),
        "combined": {
            "one_open_trade_at_a_time": True,
            "normal_position_sizing": (
                "90_PERCENT_CURRENT_EQUITY"
            ),
            "hero_zero_capital_cap": (
                _OKAI_HERO_CAPITAL_CAP
            ),
            "overlapping_trade_skipped": True,
        },
        "summary": {
            "period": "DAILY",
            "strategy_mode": "COMBINED",
            "trades": len(selected),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "capital": round(
                float(capital),
                2,
            ),
            "ending_capital": round(
                float(capital) + total_pnl,
                2,
            ),
            "net_pnl": total_pnl,
            "normal_pnl": normal_pnl,
            "hero_zero_pnl": hero_pnl,
            "note": (
                "Normal and Hero Zero are merged "
                "chronologically with one open trade at a time."
            ),
        },
    }


def _okai_run_backtest_mode(
    broker_name,
    obj,
    instrument,
    date_str,
    capital,
    entry_threshold=82,
    sl_percent=0.0,
    target_percent=0.0,
    strategy_mode="NORMAL",
):
    mode = _okai_normalize_strategy_mode(
        strategy_mode
    )
    instrument = str(
        instrument or "AUTO"
    ).upper()

    if mode == "HERO_ZERO":
        return _okai_run_hero_zero_day(
            broker_name,
            obj,
            instrument,
            date_str,
            capital,
        )

    normal_result = run_realistic_day_backtest(
        broker_name,
        obj,
        instrument,
        date_str,
        capital,
        82,
        sl_percent,
        target_percent,
    )

    if isinstance(normal_result, dict):
        normal_result["strategy_mode"] = (
            "NORMAL"
        )
        normal_result["normal_pnl"] = float(
            normal_result.get(
                "total_pnl",
                0,
            )
            or 0
        )
        normal_result["hero_zero_pnl"] = 0.0

        if isinstance(
            normal_result.get("summary"),
            dict,
        ):
            normal_result["summary"][
                "strategy_mode"
            ] = "NORMAL"
            normal_result["summary"][
                "normal_pnl"
            ] = normal_result[
                "normal_pnl"
            ]
            normal_result["summary"][
                "hero_zero_pnl"
            ] = 0.0

        for trade in normal_result.get(
            "trades",
            [],
        ):
            trade["strategy"] = "NORMAL"

    if mode == "NORMAL":
        return normal_result

    hero_result = _okai_run_hero_zero_day(
        broker_name,
        obj,
        instrument,
        date_str,
        capital,
    )

    return _okai_combine_normal_and_hero(
        normal_result,
        hero_result,
        capital,
        instrument,
        date_str,
    )



# ============================================================
# OKAI MONTHLY ASYNC JOB V1
# The app starts a job and polls until the result is complete.
# ============================================================
_OKAI_MONTHLY_JOBS = {}
_OKAI_MONTHLY_JOBS_LOCK = threading.Lock()
_OKAI_MONTHLY_MAX_JOBS = 40


def _okai_update_monthly_job(
    job_id,
    **updates,
):
    if not job_id:
        return

    with _OKAI_MONTHLY_JOBS_LOCK:
        job = _OKAI_MONTHLY_JOBS.get(
            job_id
        )
        if not job:
            return

        job.update(updates)
        job["updated_at"] = (
            datetime.now(timezone.utc)
            .isoformat()
        )


def _okai_trim_monthly_jobs():
    with _OKAI_MONTHLY_JOBS_LOCK:
        if (
            len(_OKAI_MONTHLY_JOBS)
            <= _OKAI_MONTHLY_MAX_JOBS
        ):
            return

        ordered = sorted(
            _OKAI_MONTHLY_JOBS.items(),
            key=lambda item: item[1].get(
                "created_at",
                "",
            ),
        )

        remove_count = (
            len(_OKAI_MONTHLY_JOBS)
            - _OKAI_MONTHLY_MAX_JOBS
        )

        for job_id, _ in ordered[
            :remove_count
        ]:
            _OKAI_MONTHLY_JOBS.pop(
                job_id,
                None,
            )


def _okai_monthly_worker(
    job_id,
    body,
    authorization,
):
    _okai_update_monthly_job(
        job_id,
        status="RUNNING",
        phase="LOGIN_AND_DATA",
    )

    worker_body = dict(body or {})
    worker_body[
        "_monthly_job_id"
    ] = job_id

    try:
        result = (
            _okai_run_monthly_backtest_sync(
                worker_body,
                authorization,
            )
        )

        if (
            isinstance(result, dict)
            and result.get("success")
        ):
            _okai_update_monthly_job(
                job_id,
                status="COMPLETED",
                phase="COMPLETED",
                result=result,
                completed_days=(
                    result.get(
                        "tested_days",
                        0,
                    )
                    + result.get(
                        "skipped_days",
                        0,
                    )
                ),
            )
        else:
            _okai_update_monthly_job(
                job_id,
                status="FAILED",
                phase="FAILED",
                error=(
                    result.get("message")
                    if isinstance(
                        result,
                        dict,
                    )
                    else "Monthly backtest failed"
                ),
                result=result,
            )

    except Exception as exc:
        _okai_update_monthly_job(
            job_id,
            status="FAILED",
            phase="FAILED",
            error=str(exc)[:300],
            result={
                "success": False,
                "message": (
                    "Monthly background job failed."
                ),
                "error": str(exc),
                "error_type": (
                    exc.__class__.__name__
                ),
            },
        )


@router.post("/monthly")
def start_monthly_backtest(
    body: dict,
    authorization: str = Header(None),
):
    try:
        user = get_current_user(
            authorization
        )
        body = body or {}

        with _OKAI_MONTHLY_JOBS_LOCK:
            for existing_id, existing in (
                _OKAI_MONTHLY_JOBS.items()
            ):
                if (
                    existing.get("user_id")
                    == user["id"]
                    and existing.get("status")
                    in ("QUEUED", "RUNNING")
                ):
                    return {
                        "success": True,
                        "async": True,
                        "job_id": existing_id,
                        "status": existing.get(
                            "status"
                        ),
                        "message": (
                            "Monthly backtest already "
                            "running."
                        ),
                    }

        job_id = uuid.uuid4().hex
        created_at = (
            datetime.now(timezone.utc)
            .isoformat()
        )

        month_text = str(
            body.get("month")
            or body.get("year_month")
            or ""
        )
        instrument = str(
            body.get("instrument")
            or "AUTO"
        ).upper()
        strategy_mode = str(
            body.get("strategy_mode")
            or "NORMAL"
        ).upper()

        with _OKAI_MONTHLY_JOBS_LOCK:
            _OKAI_MONTHLY_JOBS[
                job_id
            ] = {
                "job_id": job_id,
                "user_id": user["id"],
                "status": "QUEUED",
                "phase": "QUEUED",
                "month": month_text,
                "instrument": instrument,
                "strategy_mode": strategy_mode,
                "completed_days": 0,
                "total_days": 0,
                "current_date": None,
                "created_at": created_at,
                "updated_at": created_at,
                "result": None,
                "error": None,
            }

        thread = threading.Thread(
            target=_okai_monthly_worker,
            args=(
                job_id,
                dict(body),
                authorization,
            ),
            daemon=True,
            name=(
                "okai-monthly-"
                + job_id[:8]
            ),
        )
        thread.start()

        _okai_trim_monthly_jobs()

        return {
            "success": True,
            "async": True,
            "job_id": job_id,
            "status": "QUEUED",
            "month": month_text,
            "instrument": instrument,
            "strategy_mode": strategy_mode,
            "message": (
                "Monthly backtest background me "
                "start ho gaya."
            ),
        }

    except Exception as exc:
        return {
            "success": False,
            "message": (
                "Monthly job start failed."
            ),
            "error": str(exc),
            "error_type": (
                exc.__class__.__name__
            ),
        }


@router.get("/monthly/status/{job_id}")
def monthly_backtest_status(
    job_id: str,
    authorization: str = Header(None),
):
    try:
        user = get_current_user(
            authorization
        )

        with _OKAI_MONTHLY_JOBS_LOCK:
            job = _OKAI_MONTHLY_JOBS.get(
                job_id
            )

            if not job:
                return {
                    "success": False,
                    "status": "NOT_FOUND",
                    "message": (
                        "Monthly job nahi mila "
                        "ya server restart ho gaya."
                    ),
                }

            if job.get("user_id") != user["id"]:
                return {
                    "success": False,
                    "status": "FORBIDDEN",
                    "message": "Access denied.",
                }

            response = {
                key: value
                for key, value in job.items()
                if key != "user_id"
            }

        response["success"] = (
            response.get("status")
            != "FAILED"
        )
        return _json_safe(response)

    except Exception as exc:
        return {
            "success": False,
            "status": "FAILED",
            "message": (
                "Monthly status check failed."
            ),
            "error": str(exc),
        }
