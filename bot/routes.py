from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from bot.angel_fetcher import start_user_bot, stop_user_bot, get_user_bot_state
from bot.strategy import is_hero_window_active
from telegram.routes import notify_user
from datetime import datetime, timedelta
import json
import random

router = APIRouter(prefix="/bot", tags=["Bot"])

def get_strategy_settings(conn, user_id: int):
    default = {
        "trading_mode": "paper",
        "paper_capital": 100000,
        "mode": "default"
    }
    try:
        row = conn.execute(
            "SELECT settings_json FROM strategy_settings WHERE user_id=?",
            (user_id,)
        ).fetchone()
        if row:
            saved = json.loads(row["settings_json"])
            default.update(saved)
    except Exception:
        pass
    return default

def save_bot_status(conn, user_id: int, is_running: int, last_signal: str = "WAITING"):
    now = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT INTO bot_status
           (user_id, is_running, last_signal, total_trades, total_pnl, updated_at)
           VALUES (?, ?, ?, 0, 0, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             is_running=excluded.is_running,
             last_signal=excluded.last_signal,
             updated_at=excluded.updated_at""",
        (user_id, is_running, last_signal, now)
    )
    conn.commit()

@router.get("/signal")
def get_signal(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    settings = get_strategy_settings(conn, user["id"])

    # ensure paper trade table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT,
            side TEXT,
            entry_price REAL,
            exit_price REAL,
            qty INTEGER DEFAULT 0,
            pnl REAL DEFAULT 0,
            status TEXT DEFAULT 'OPEN',
            reason TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    row = conn.execute(
        "SELECT * FROM bot_status WHERE user_id=?",
        (user["id"],)
    ).fetchone()

    is_running = False
    last_signal = "WAITING"
    total_trades = 0
    total_pnl = 0
    updated_at = None

    if row:
        is_running = bool(row["is_running"])
        last_signal = row["last_signal"] or "WAITING"
        total_trades = row["total_trades"] or 0
        total_pnl = row["total_pnl"] or 0
        updated_at = row["updated_at"]

    trading_mode = settings.get("trading_mode", "paper")
    paper_capital = float(settings.get("paper_capital", 100000) or 100000)
    primary = settings.get("primary_instrument", "NIFTY")
    enabled = settings.get("enabled_instruments", ["NIFTY"])

    entry_threshold = int(settings.get("entry_threshold", 82))
    adx_threshold = int(settings.get("adx_threshold", 25))
    volume_threshold = float(settings.get("volume_threshold", 1.2))

    now = datetime.utcnow()
    minute_seed = int(now.strftime("%H%M"))
    random.seed(f"{user['id']}-{primary}-{minute_seed}")

    # Dynamic paper values for testing
    if trading_mode == "paper" and is_running:
        adx = round(random.uniform(18, 38), 1)
        volume_ratio = round(random.uniform(0.8, 2.2), 2)
        mtf_ok = random.choice([True, True, False])

        base_score = 40 if random.random() > 0.25 else 0
        adx_score = int(settings.get("adx_score", 20)) if adx >= adx_threshold else 0
        volume_score = int(settings.get("volume_score", 20)) if volume_ratio >= volume_threshold else 0
        mtf_score = int(settings.get("mtf_score", 10)) if mtf_ok else 0
        regime_score = int(settings.get("regime_score", 10)) if random.random() > 0.35 else 0

        score = base_score + adx_score + volume_score + mtf_score + regime_score
        score = min(100, max(0, score))

        side = "CE" if random.random() > 0.5 else "PE"
        symbol = f"{primary} PAPER {side}"
        signal = "BUY_" + side if score >= entry_threshold else "WAITING"
        status = "PAPER_RUNNING"

        # Create paper trade only if score passes and no very recent open trade
        recent = conn.execute(
            """SELECT id FROM paper_trades
               WHERE user_id=? AND status='OPEN'
               ORDER BY id DESC LIMIT 1""",
            (user["id"],)
        ).fetchone()

        if score >= entry_threshold and not recent:
            entry_price = round(random.uniform(90, 180), 2)
            qty = 50 if primary == "NIFTY" else 15
            conn.execute(
                """INSERT INTO paper_trades
                   (user_id, symbol, side, entry_price, qty, pnl, status, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, 'OPEN', ?, ?)""",
                (
                    user["id"],
                    symbol,
                    side,
                    entry_price,
                    qty,
                    f"Paper entry score {score}/{entry_threshold}",
                    datetime.utcnow().isoformat()
                )
            )
            conn.execute(
                """INSERT INTO bot_status
                   (user_id, is_running, last_signal, total_trades, total_pnl, updated_at)
                   VALUES (?, 1, ?, 1, 0, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     is_running=1,
                     last_signal=excluded.last_signal,
                     total_trades=bot_status.total_trades + 1,
                     updated_at=excluded.updated_at""",
                (user["id"], signal, datetime.utcnow().isoformat())
            )
            conn.commit()

            try:
                notify_user(
                    user["id"],
                    f"📝 <b>Paper Trade Entry</b>
"
                    f"Symbol: {symbol}
"
                    f"Side: {side}
"
                    f"Entry: ₹{entry_price}
"
                    f"Score: {score}/{entry_threshold}"
                )
            except Exception:
                pass

        mtf = "OK" if mtf_ok else "WEAK"

    else:
        score = 0
        signal = "WAITING"
        status = "PAPER_STOPPED" if trading_mode == "paper" else "LIVE_WAITING"
        adx = 0
        volume_ratio = 0
        mtf = "WAITING"
        base_score = adx_score = volume_score = mtf_score = regime_score = 0

    # latest totals
    try:
        total_trades = conn.execute(
            "SELECT COUNT(*) AS c FROM paper_trades WHERE user_id=?",
            (user["id"],)
        ).fetchone()["c"]
    except Exception:
        pass

    conn.close()

    return {
        "success": True,
        "running": is_running,
        "is_running": is_running,
        "status": status,
        "signal": signal,
        "last_signal": signal,
        "score": score,
        "tqu_score": score,
        "min_score": entry_threshold,

        "adx": adx,
        "adx_threshold": adx_threshold,
        "volume_ratio": volume_ratio,
        "volume_threshold": volume_threshold,
        "mtf": mtf,
        "mtf_status": mtf,

        "base_score": base_score,
        "adx_score": adx_score,
        "volume_score": volume_score,
        "mtf_score": mtf_score,
        "regime_score": regime_score,

        "trading_mode": trading_mode,
        "paper_capital": paper_capital,
        "primary_instrument": primary,
        "enabled_instruments": enabled,
        "total_trades": total_trades,
        "total_pnl": total_pnl,
        "updated_at": datetime.utcnow().isoformat(),
        "message": "Paper dynamic signal" if trading_mode == "paper" else "Live engine waiting for market data"
    }



@router.get("/hero-status")
def get_hero_status(authorization: str = Header(None)):
    get_current_user(authorization)
    return is_hero_window_active()

@router.post("/start")
def bot_start(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    settings = get_strategy_settings(conn, user["id"])
    trading_mode = settings.get("trading_mode", "paper")

    if trading_mode != "live":
        save_bot_status(conn, user["id"], 1, "PAPER_MODE")
        conn.close()
        notify_user(
            user["id"],
            f"📝 <b>Paper Bot Started</b>\n"
            f"Mode: PAPER\n"
            f"Paper Capital: ₹{settings.get('paper_capital', 100000)}\n"
            f"Instruments: {', '.join(settings.get('enabled_instruments', ['NIFTY']))}\n"
            f"Primary: {settings.get('primary_instrument', 'NIFTY')}\n"
            f"Real orders OFF."
        )
        return {
            "success": True,
            "message": "Paper mode bot started. Real orders OFF.",
            "mode": "paper",
            "paper_capital": settings.get("paper_capital", 100000)
        }

    broker = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
        (user["id"],)
    ).fetchone()
    conn.close()

    if not broker:
        return {"success": False, "message": "Live mode ke liye pehle broker credentials save karo"}

    creds = {
        "api_key": broker["api_key"],
        "client_id": broker["client_id"],
        "password": broker["api_secret"],
        "totp_secret": broker["totp_secret"],
    }

    res = start_user_bot(user["id"], creds)
    if isinstance(res, dict) and res.get("success"):
        notify_user(
            user["id"],
            f"▶️ <b>LIVE Bot Started</b>\nInstruments: {', '.join(settings.get('enabled_instruments', ['NIFTY']))}\nPrimary: {settings.get('primary_instrument', 'NIFTY')}\nReal orders enabled. Risk carefully manage karein."
        )
    return res

@router.post("/stop")
def bot_stop(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    try:
        save_bot_status(conn, user["id"], 0, "STOPPED")
    finally:
        conn.close()

    res = stop_user_bot(user["id"])
    notify_user(user["id"], "⏹️ <b>Bot Stopped</b>")
    return {"success": True, "message": "Bot stopped", "engine_response": res}

@router.post("/update-signal")
def update_signal(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    signal = body.get("signal", "UPDATE") if isinstance(body, dict) else "UPDATE"
    score = body.get("score", "--") if isinstance(body, dict) else "--"
    symbol = body.get("symbol", "--") if isinstance(body, dict) else "--"
    notify_user(user["id"], f"📢 <b>Signal Update</b>\nSignal: {signal}\nSymbol: {symbol}\nScore: {score}")
    return {"success": True}
