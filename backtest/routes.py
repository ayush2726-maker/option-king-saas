from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from auth.utils import decrypt_credential
from datetime import datetime, timezone, timedelta
import json
import math

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
    from bot.brokers.factory import create_broker
    ENGINE_AVAILABLE = True
except Exception:
    ENGINE_AVAILABLE = False


router = APIRouter(prefix="/backtest", tags=["Backtest"])

LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}


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
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().reset_index(drop=True)


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

    trades = []
    open_trade = None
    trade_no = 0
    consecutive_losses = 0
    _score_log = []
    _score_detail_log = []

    for i in range(28, len(df)):
        window = df.iloc[:i + 1].copy()
        result = calculate_indicators(window)
        if result is None:
            continue
        wdf, trend = result
        last = wdf.iloc[-1]
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
            underlying_move_pct = (price - open_trade["entry_spot"]) / open_trade["entry_spot"] * 100
            if side == "PE":
                underlying_move_pct = -underlying_move_pct
            est_premium_move_pct = underlying_move_pct * 8
            current_premium = max(0.5, entry * (1 + est_premium_move_pct / 100))

            hit_target = current_premium >= entry * (1 + target_percent / 100)
            hit_sl = current_premium <= entry * (1 - sl_percent / 100)
            is_last_candle = (i == len(df) - 1)

            if (
                hit_target
                or hit_sl
                or force_eod_exit
                or is_last_candle
            ):
                exit_price = round(current_premium, 2)
                pnl = round((exit_price - entry) * qty, 2)

                if hit_target:
                    reason = "TARGET"
                elif hit_sl:
                    reason = "SL"
                elif force_eod_exit:
                    reason = "EOD_EXIT_1525"
                else:
                    reason = "DAY_END_EXIT"
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
                })
                if pnl < 0:
                    consecutive_losses += 1
                else:
                    consecutive_losses = 0

                open_trade = None
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
            est_entry_premium = round(max(20, atr * 6), 2)
            open_trade = {
                "side": signal_data["signal"],
                "entry_price": est_entry_premium,
                "entry_spot": price,
                "score": signal_data["score"],
                "entry_time": str(last["time"]),
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

        raw_result = run_realistic_day_backtest(
            broker_name,
            obj,
            instrument,
            run_date,
            capital,
            entry_score,
            sl_percent,
            target_percent,
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
