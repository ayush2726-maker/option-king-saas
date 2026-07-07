from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from auth.utils import decrypt_credential
from bot.strategy import is_hero_window_active
from bot.option_chain import resolve_option
from telegram.routes import notify_user
from datetime import datetime, timezone, timedelta
import json
import random

try:
    from bot.angel_fetcher import start_user_bot, stop_user_bot, get_user_bot_state, angel_login, INDEX_TOKENS, INDEX_EXCHANGE
except Exception:
    def start_user_bot(user_id, creds):
        return {"success": False, "message": "Live engine unavailable"}

    def stop_user_bot(user_id):
        return {"success": True, "message": "Stopped"}


router = APIRouter(prefix="/bot", tags=["Bot"])


DEFAULT_SETTINGS = {
    "mode": "default",
    "base_score": 40,
    "adx_score": 20,
    "volume_score": 20,
    "mtf_score": 10,
    "regime_score": 10,
    "entry_threshold": 82,
    "adx_threshold": 25,
    "volume_threshold": 1.2,
    "max_trades_per_day": 5,
    "sl_percent": 12,
    "target_percent": 24,
    "trailing_sl": True,
    "expiry_gamma_mode": True,
    "trading_mode": "paper",
    "paper_capital": 100000,
    "primary_instrument": "NIFTY",
    "enabled_instruments": ["NIFTY", "BANKNIFTY", "SENSEX"],
}


LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
}


PAPER_BASE_LEVELS = {
    "NIFTY": 25000,
    "BANKNIFTY": 57000,
    "SENSEX": 82000,
}

PAPER_STRIKE_STEP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "SENSEX": 100,
}


def next_weekly_expiry_label():
    # Paper/demo expiry label only. Real expiry will come from broker option chain later.
    today = datetime.utcnow().date()
    days_ahead = (3 - today.weekday()) % 7  # Thursday style demo expiry
    if days_ahead == 0:
        days_ahead = 7
    expiry = today.fromordinal(today.toordinal() + days_ahead)
    return expiry.strftime("%d%b").upper()


def make_paper_option_symbol(primary: str, side: str):
    base = PAPER_BASE_LEVELS.get(primary, 25000)
    step = PAPER_STRIKE_STEP.get(primary, 50)

    # small simulated ATM/OTM shift
    shift = random.choice([-2, -1, 0, 1, 2]) * step
    strike = base + shift

    expiry = next_weekly_expiry_label()
    display_symbol = f"{primary} {expiry} {strike} {side}"
    broker_symbol = f"{primary} PAPER {expiry} {strike}{side}"

    return display_symbol, broker_symbol, strike, expiry


def ensure_tables(conn):
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_status (
            user_id INTEGER PRIMARY KEY,
            is_running INTEGER DEFAULT 0,
            last_signal TEXT DEFAULT 'WAITING',
            total_trades INTEGER DEFAULT 0,
            total_pnl REAL DEFAULT 0,
            updated_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_bot_state (
            user_id INTEGER PRIMARY KEY,
            is_running INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_history (
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

    for col, coltype in [("sl_price","REAL"), ("target_price","REAL"), ("token","TEXT"), ("exch_seg","TEXT"), ("expiry","TEXT"), ("strike","REAL")]:
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {coltype}")
        except Exception:
            pass

    conn.commit()


def log_signal_snapshot(conn, user_id: int, engine_state: dict):
    """
    Logs a new signal_history row only when the engine has produced a
    genuinely new candle update (dedup via engine_updated_at), so charts
    get real time-series data without flooding the table on every poll.
    """
    try:
        engine_updated_at = engine_state.get("updated_at")
        if not engine_updated_at:
            return

        last = conn.execute(
            "SELECT engine_updated_at FROM signal_history WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()

        if last and last["engine_updated_at"] == engine_updated_at:
            return

        conn.execute(
            """INSERT INTO signal_history
               (user_id, price, score, signal, adx, volume_ratio, engine_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                float(engine_state.get("price", 0) or 0),
                int(engine_state.get("score", 0) or 0),
                str(engine_state.get("signal", "")),
                float(engine_state.get("adx", 0) or 0),
                float(engine_state.get("volume_ratio", 0) or 0),
                engine_updated_at,
            )
        )

        conn.execute(
            """DELETE FROM signal_history WHERE user_id=? AND id NOT IN (
                   SELECT id FROM signal_history WHERE user_id=? ORDER BY id DESC LIMIT 200
               )""",
            (user_id, user_id)
        )

        conn.commit()
    except Exception:
        pass

def get_strategy_settings(conn, user_id: int):
    settings = dict(DEFAULT_SETTINGS)

    try:
        row = conn.execute(
            "SELECT settings_json FROM strategy_settings WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if row:
            saved = json.loads(row["settings_json"])
            settings.update(saved)
    except Exception:
        pass

    settings.setdefault("trading_mode", "paper")
    settings.setdefault("paper_capital", 100000)
    settings.setdefault("primary_instrument", "NIFTY")
    settings.setdefault("enabled_instruments", ["NIFTY", "BANKNIFTY", "SENSEX"])

    return settings


def save_bot_status(conn, user_id: int, is_running: int, last_signal: str):
    now = datetime.utcnow().isoformat()

    cur = conn.execute(
        """UPDATE bot_status
           SET is_running=?, last_signal=?, updated_at=?
           WHERE user_id=?""",
        (is_running, last_signal, now, user_id)
    )

    if cur.rowcount == 0:
        conn.execute(
            """INSERT INTO bot_status
               (user_id, is_running, last_signal, total_trades, total_pnl, updated_at)
               VALUES (?, ?, ?, 0, 0, ?)""",
            (user_id, is_running, last_signal, now)
        )

    conn.execute(
        "INSERT OR REPLACE INTO user_bot_state (user_id, is_running, updated_at) VALUES (?, ?, ?)",
        (user_id, is_running, now)
    )

    conn.commit()


def add_trade_count(conn, user_id: int, signal: str):
    now = datetime.utcnow().isoformat()

    cur = conn.execute(
        """UPDATE bot_status
           SET is_running=1,
               last_signal=?,
               total_trades=COALESCE(total_trades, 0) + 1,
               updated_at=?
           WHERE user_id=?""",
        (signal, now, user_id)
    )

    if cur.rowcount == 0:
        conn.execute(
            """INSERT INTO bot_status
               (user_id, is_running, last_signal, total_trades, total_pnl, updated_at)
               VALUES (?, 1, ?, 1, 0, ?)""",
            (user_id, signal, now)
        )

    conn.commit()


def add_pnl(conn, user_id: int, pnl: float):
    now = datetime.utcnow().isoformat()

    cur = conn.execute(
        """UPDATE bot_status
           SET is_running=1,
               last_signal='PAPER_EXIT',
               total_pnl=COALESCE(total_pnl, 0) + ?,
               updated_at=?
           WHERE user_id=?""",
        (pnl, now, user_id)
    )

    if cur.rowcount == 0:
        conn.execute(
            """INSERT INTO bot_status
               (user_id, is_running, last_signal, total_trades, total_pnl, updated_at)
               VALUES (?, 1, 'PAPER_EXIT', 0, ?, ?)""",
            (user_id, pnl, now)
        )

    conn.commit()


@router.get("/signal")
def get_signal(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    ensure_tables(conn)

    settings = get_strategy_settings(conn, user["id"])

    row = conn.execute(
        "SELECT * FROM bot_status WHERE user_id=?",
        (user["id"],)
    ).fetchone()

    is_running = bool(row["is_running"]) if row else False

    try:
        state_row = conn.execute(
            "SELECT is_running FROM user_bot_state WHERE user_id=?",
            (user["id"],)
        ).fetchone()
        if state_row:
            is_running = bool(state_row["is_running"])
    except Exception:
        pass

    trading_mode = settings.get("trading_mode", "paper")
    paper_capital = float(settings.get("paper_capital", 100000) or 100000)
    primary = settings.get("primary_instrument", "NIFTY")
    enabled = settings.get("enabled_instruments", ["NIFTY", "BANKNIFTY", "SENSEX"])

    entry_threshold = int(settings.get("entry_threshold", 82))
    adx_threshold = int(settings.get("adx_threshold", 25))
    volume_threshold = float(settings.get("volume_threshold", 1.2))
    sl_percent = float(settings.get("sl_percent", 12))
    target_percent = float(settings.get("target_percent", 24))

    qty = LOT_SIZES.get(primary, 1)
    now = datetime.utcnow()

    # Existing open paper trade exit check
    open_trade = conn.execute(
        """SELECT * FROM paper_trades
           WHERE user_id=? AND status='OPEN'
           ORDER BY id DESC LIMIT 1""",
        (user["id"],)
    ).fetchone()

    if trading_mode == "paper" and is_running and open_trade:
        # Correct old wrong qty also
        if int(open_trade["qty"] or 0) != qty:
            conn.execute(
                "UPDATE paper_trades SET qty=? WHERE id=?",
                (qty, open_trade["id"])
            )
            conn.commit()

        entry_price = float(open_trade["entry_price"] or 0)
        old_qty = qty

        trade_text = (str(open_trade["symbol"] or "") + " " + str(open_trade["reason"] or "")).upper()
        trade_sl_percent = sl_percent
        trade_target_percent = target_percent

        if "HEROZERO" in trade_text or "HERO ZERO" in trade_text:
            trade_sl_percent = 50
            trade_target_percent = 100

        random.seed(f"exit-{user['id']}-{open_trade['id']}-{now.strftime('%H%M%S')}")

        if "HEROZERO" in trade_text or "HERO ZERO" in trade_text:
            move_pct = random.uniform(-0.70, 1.30)
        else:
            move_pct = random.uniform(-0.18, 0.30)

        current_price = round(entry_price * (1 + move_pct), 2)
        pnl = round((current_price - entry_price) * old_qty, 2)

        age_min = 0
        try:
            created = datetime.fromisoformat(open_trade["created_at"])
            age_min = (now - created).total_seconds() / 60
        except Exception:
            pass

        exit_reason = None

        if move_pct * 100 >= trade_target_percent:
            exit_reason = f"TARGET HIT {round(move_pct * 100, 2)}%"
        elif move_pct * 100 <= -trade_sl_percent:
            exit_reason = f"SL HIT {round(move_pct * 100, 2)}%"
        elif age_min >= 10:
            exit_reason = f"TIME EXIT {round(move_pct * 100, 2)}%"

        if False and exit_reason:
            conn.execute(
                """UPDATE paper_trades
                   SET exit_price=?, pnl=?, status='CLOSED', reason=?
                   WHERE id=?""",
                (current_price, pnl, exit_reason, open_trade["id"])
            )

            add_pnl(conn, user["id"], pnl)

            try:
                msg = "\n".join([
                    "📤 <b>Paper Trade Exit</b>",
                    f"Symbol: {open_trade['symbol']}",
                    f"Qty: {old_qty}",
                    f"Exit: ₹{current_price}",
                    f"P&L: ₹{pnl}",
                    f"Reason: {exit_reason}",
                ])
                notify_user(user["id"], msg)
            except Exception:
                pass

            open_trade = None

    # Dynamic signal - REAL ENGINE (TQU strategy.py) when broker connected
    if is_running:
        engine_state = get_user_bot_state(user["id"])
        engine_ready = engine_state.get("strategy") == "TQU_ENHANCED"

        if engine_ready:
            score = int(engine_state.get("score", 0))
            log_signal_snapshot(conn, user["id"], engine_state)
            adx = float(engine_state.get("adx", 0))
            volume_ratio = float(engine_state.get("volume_ratio", 0))
            mtf_ok = bool(engine_state.get("mtf_confirmed", False))
            base_score = int(engine_state.get("base_score", 0))
            adx_score = int(engine_state.get("adx_bonus", 0))
            volume_score = int(engine_state.get("volume_bonus", 0))
            mtf_score = int(engine_state.get("mtf_bonus", 0))
            regime_score = int(engine_state.get("regime_score", 0))
            side = engine_state.get("signal", "WAIT")
            if side not in ("CE", "PE"):
                side = "CE"
            display_symbol, broker_symbol, strike, expiry = make_paper_option_symbol(primary, side)
            symbol = display_symbol

            if open_trade:
                signal = "HOLD_" + str(open_trade["side"])
            else:
                signal = "READY_" + side if score >= entry_threshold else "WAITING"

            status = "PAPER_RUNNING" if trading_mode == "paper" else "LIVE_RUNNING"
            mtf = "OK" if mtf_ok else "WEAK"

            open_trade = conn.execute(
                """SELECT id FROM paper_trades
                   WHERE user_id=? AND status='OPEN'
                   ORDER BY id DESC LIMIT 1""",
                (user["id"],)
            ).fetchone()
        else:
            score = 0
            signal = "NO_DATA"
            status = "CONNECT_BROKER_FOR_REAL_SIGNAL"
            adx = 0
            volume_ratio = 0
            mtf = "WAITING"
            base_score = 0
            adx_score = 0
            volume_score = 0
            mtf_score = 0
            regime_score = 0
            symbol = None
    else:
        score = 0
        signal = "WAITING"
        status = "PAPER_STOPPED" if trading_mode == "paper" else "LIVE_WAITING"
        adx = 0
        volume_ratio = 0
        mtf = "WAITING"
        base_score = 0
        adx_score = 0
        volume_score = 0
        mtf_score = 0
        regime_score = 0
        symbol = None

    total_trades = 0
    total_pnl = 0
    active_trade = None
    latest_trade = None

    try:
        total_trades = conn.execute(
            "SELECT COUNT(*) AS c FROM paper_trades WHERE user_id=?",
            (user["id"],)
        ).fetchone()["c"]

        total_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) AS p FROM paper_trades WHERE user_id=?",
            (user["id"],)
        ).fetchone()["p"]

        open_row = conn.execute(
            """SELECT * FROM paper_trades
               WHERE user_id=? AND status='OPEN'
               ORDER BY id DESC LIMIT 1""",
            (user["id"],)
        ).fetchone()

        latest_row = conn.execute(
            """SELECT * FROM paper_trades
               WHERE user_id=?
               ORDER BY id DESC LIMIT 1""",
            (user["id"],)
        ).fetchone()

        def make_trade_view(t):
            if not t:
                return None

            entry = float(t["entry_price"] or 0)
            exit_p = t["exit_price"]
            qty_v = int(t["qty"] or qty)

            trade_text = (str(t["symbol"] or "") + " " + str(t["reason"] or "")).upper()
            local_sl_percent = sl_percent
            local_target_percent = target_percent

            if "HEROZERO" in trade_text or "HERO ZERO" in trade_text:
                local_sl_percent = 50
                local_target_percent = 100

            sl_price = round(entry * (1 - local_sl_percent / 100), 2) if entry else None
            target_price = round(entry * (1 + local_target_percent / 100), 2) if entry else None

            current_price = None
            unrealized_pnl = None

            if str(t["status"]).upper() == "OPEN":
                random.seed(f"view-{user['id']}-{t['id']}-{datetime.utcnow().strftime('%H%M%S')}")

                if "HEROZERO" in trade_text or "HERO ZERO" in trade_text:
                    move_pct_view = random.uniform(-0.60, 1.10)
                else:
                    move_pct_view = random.uniform(-0.08, 0.18)

                current_price = round(entry * (1 + move_pct_view), 2)
                unrealized_pnl = round((current_price - entry) * qty_v, 2)

            return {
                "id": t["id"],
                "symbol": t["symbol"],
                "side": t["side"],
                "qty": qty_v,
                "entry_price": entry,
                "current_price": current_price,
                "sl_price": sl_price,
                "target_price": target_price,
                "exit_price": float(exit_p) if exit_p is not None else None,
                "pnl": float(t["pnl"] or 0),
                "unrealized_pnl": unrealized_pnl,
                "status": t["status"],
                "reason": t["reason"],
                "created_at": t["created_at"],
            }

        active_trade = make_trade_view(open_row)
        latest_trade = make_trade_view(latest_row)

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
        "qty": qty,
        "active_trade": active_trade,
        "latest_trade": latest_trade,
        "trade_symbol": active_trade.get("symbol") if active_trade else None,
        "trade_side": active_trade.get("side") if active_trade else None,
        "trade_qty": active_trade.get("qty") if active_trade else qty,
        "entry_price": active_trade.get("entry_price") if active_trade else None,
        "current_price": active_trade.get("current_price") if active_trade else None,
        "sl_price": active_trade.get("sl_price") if active_trade else None,
        "target_price": active_trade.get("target_price") if active_trade else None,
        "exit_price": active_trade.get("exit_price") if active_trade else None,
        "trade_pnl": active_trade.get("unrealized_pnl") if active_trade else None,
        "total_trades": total_trades,
        "total_pnl": round(float(total_pnl or 0), 2),
        "updated_at": datetime.utcnow().isoformat(),
        "message": "Signal view-only. App refresh trade create/exit nahi karega. Button se trade start/close hoga."
    }


@router.get("/debug-state")
def debug_bot_state(authorization: str = Header(None)):
    user = get_current_user(authorization)
    from bot.angel_fetcher import get_user_bot_state
    return get_user_bot_state(user["id"])

@router.get("/hero-status")
def get_hero_status(authorization: str = Header(None)):
    get_current_user(authorization)
    return is_hero_window_active()


@router.post("/start")
def bot_start(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    ensure_tables(conn)

    settings = get_strategy_settings(conn, user["id"])
    trading_mode = settings.get("trading_mode", "paper")

    if trading_mode != "live":
        save_bot_status(conn, user["id"], 1, "PAPER_MODE")

        broker = conn.execute(
            "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
            (user["id"],)
        ).fetchone()
        conn.close()

        engine_note = "Broker connect karein real TQU signal ke liye."
        if broker:
            try:
                creds = {
                    "api_key": decrypt_credential(broker["api_key"]),
                    "client_id": broker["client_id"],
                    "password": decrypt_credential(broker["api_secret"]),
                    "totp_secret": decrypt_credential(broker["totp_secret"]) if broker["totp_secret"] else None,
                }
                start_user_bot(user["id"], creds)
                engine_note = "Real TQU signal engine started (paper mode - real orders OFF)."
            except Exception as e:
                engine_note = f"DEBUG_ERROR: {str(e)[:200]}"

        try:
            msg = "\n".join([
                "📝 <b>Paper Bot Started</b>",
                "Mode: PAPER",
                f"Paper Capital: ₹{settings.get('paper_capital', 100000)}",
                f"Instruments: {', '.join(settings.get('enabled_instruments', ['NIFTY']))}",
                f"Primary: {settings.get('primary_instrument', 'NIFTY')}",
                "Real orders OFF.",
            ])
            notify_user(user["id"], msg)
        except Exception:
            pass

        return {
            "success": True,
            "message": f"Paper mode bot started. Real orders OFF. {engine_note}",
            "mode": "paper",
            "paper_capital": settings.get("paper_capital", 100000),
            "primary_instrument": settings.get("primary_instrument", "NIFTY"),
            "enabled_instruments": settings.get("enabled_instruments", ["NIFTY"]),
        }

    broker = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
        (user["id"],)
    ).fetchone()

    conn.close()

    if not broker:
        return {"success": False, "message": "Live mode ke liye pehle broker credentials save karo"}

    creds = {
        "api_key": decrypt_credential(broker["api_key"]),
        "client_id": broker["client_id"],
        "password": decrypt_credential(broker["api_secret"]),
        "totp_secret": decrypt_credential(broker["totp_secret"]) if broker["totp_secret"] else None,
    }

    res = start_user_bot(user["id"], creds)
    if isinstance(res, dict) and res.get("success"):
        try:
            notify_user(user["id"], "▶️ <b>LIVE Bot Started</b>\nReal orders enabled.")
        except Exception:
            pass

    return res

@router.post("/stop")
def bot_stop(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    ensure_tables(conn)
    save_bot_status(conn, user["id"], 0, "STOPPED")
    conn.close()

    try:
        stop_user_bot(user["id"])
    except Exception:
        pass

    try:
        notify_user(user["id"], "⏹️ <b>Bot Stopped</b>")
    except Exception:
        pass

    return {"success": True, "message": "Bot stopped"}


@router.post("/update-signal")
def update_signal(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)

    signal = body.get("signal", "UPDATE") if isinstance(body, dict) else "UPDATE"
    score = body.get("score", "--") if isinstance(body, dict) else "--"
    symbol = body.get("symbol", "--") if isinstance(body, dict) else "--"

    try:
        msg = "\n".join([
            "📢 <b>Signal Update</b>",
            f"Signal: {signal}",
            f"Symbol: {symbol}",
            f"Score: {score}",
        ])
        notify_user(user["id"], msg)
    except Exception:
        pass

    return {"success": True}


def market_open() -> bool:
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return False
    start = ist.replace(hour=9, minute=15, second=0, microsecond=0)
    end = ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= ist <= end


@router.post("/hero-zero/start")
def hero_zero_start(body: dict = None, authorization: str = Header(None)):
    user = get_current_user(authorization)

    if not market_open():
        return {"success": False, "message": "Market closed. Hero Zero available only during market hours (Mon-Fri 09:15-15:30 IST)."}

    conn = get_db()
    ensure_tables(conn)

    settings = get_strategy_settings(conn, user["id"])
    primary = settings.get("primary_instrument", "NIFTY")
    qty = LOT_SIZES.get(primary, 65)

    open_trade = conn.execute(
        """SELECT * FROM paper_trades
           WHERE user_id=? AND status='OPEN'
           ORDER BY id DESC LIMIT 1""",
        (user["id"],)
    ).fetchone()

    if open_trade:
        conn.close()
        return {
            "success": True,
            "message": "Already open trade hai. Pehle old trade close hone do.",
            "active_trade": {
                "id": open_trade["id"],
                "symbol": open_trade["symbol"],
                "side": open_trade["side"],
                "qty": open_trade["qty"],
                "entry_price": open_trade["entry_price"],
                "status": open_trade["status"],
                "reason": open_trade["reason"],
                "created_at": open_trade["created_at"],
            }
        }

    broker = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
        (user["id"],)
    ).fetchone()

    if not broker:
        conn.close()
        return {"success": False, "message": "Broker connect karein Hero Zero ke liye (real premium chahiye)."}

    body = body or {}
    side = body.get("side")
    if side not in ("CE", "PE"):
        conn.close()
        return {"success": False, "message": "side CE ya PE hona chahiye"}

    try:
        creds = {
            "api_key": decrypt_credential(broker["api_key"]),
            "client_id": broker["client_id"],
            "password": decrypt_credential(broker["api_secret"]),
            "totp_secret": decrypt_credential(broker["totp_secret"]) if broker["totp_secret"] else None,
        }
        obj = angel_login(creds)

        index_token = INDEX_TOKENS.get(primary, "26000")
        index_exch = INDEX_EXCHANGE.get(primary, "NSE")
        spot_quote = obj.ltpData(index_exch, primary, index_token)
        spot_price = float(spot_quote["data"]["ltp"])

        resolved = resolve_option(primary, spot_price, side)
        if not resolved:
            conn.close()
            return {"success": False, "message": "Option contract resolve nahi hua"}

        quote = obj.ltpData(resolved["exch_seg"], resolved["symbol"], resolved["token"])
        entry_price = float(quote["data"]["ltp"])
    except Exception as e:
        conn.close()
        return {"success": False, "message": f"Real premium fetch failed: {str(e)[:150]}"}

    if entry_price <= 0:
        conn.close()
        return {"success": False, "message": "Invalid premium mila broker se"}

    symbol = resolved["symbol"]
    sl_price = round(entry_price * 0.50, 2)
    target_price = round(entry_price * 2.00, 2)

    now = datetime.utcnow().isoformat()

    conn.execute(
        """INSERT INTO paper_trades
           (user_id, symbol, side, entry_price, qty, pnl, status, reason,
            sl_price, target_price, token, exch_seg, expiry, strike, created_at)
           VALUES (?, ?, ?, ?, ?, 0, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user["id"], symbol, side, entry_price, qty,
            f"Hero Zero real entry | SL {sl_price} | Target {target_price}",
            sl_price, target_price, resolved["token"], resolved["exch_seg"],
            resolved["expiry"], resolved["strike"], now
        )
    )

    add_trade_count(conn, user["id"], "HERO_ZERO_" + side)
    save_bot_status(conn, user["id"], 1, "HERO_ZERO_" + side)

    trade_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.commit()
    conn.close()

    try:
        msg = "\n".join([
            "🚀 <b>Expiry Hero Zero Started (REAL premium)</b>",
            f"Symbol: {symbol}",
            f"Side: {side}",
            f"Qty: {qty}",
            f"Entry: Rs {entry_price}",
            f"SL: Rs {sl_price}",
            f"Target: Rs {target_price}",
            "Mode: PAPER / DEMO (real premium tracking)",
        ])
        notify_user(user["id"], msg)
    except Exception:
        pass

    return {
        "success": True,
        "message": "Expiry Hero Zero paper trade started (real premium)",
        "active_trade": {
            "id": trade_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "target_price": target_price,
            "exit_price": None,
            "pnl": 0,
            "status": "OPEN",
            "reason": f"Hero Zero real entry | SL {sl_price} | Target {target_price}",
            "created_at": now
        }
    }


@router.post("/hero-zero/force-close")
def hero_zero_force_close(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    ensure_tables(conn)

    open_trade = conn.execute(
        """SELECT * FROM paper_trades
           WHERE user_id=? AND status='OPEN'
           ORDER BY id DESC LIMIT 1""",
        (user["id"],)
    ).fetchone()

    if not open_trade:
        conn.close()
        return {"success": True, "message": "No open paper trade"}

    entry = float(open_trade["entry_price"] or 0)
    qty = int(open_trade["qty"] or 65)
    token = open_trade["token"]
    symbol = open_trade["symbol"]
    exch_seg = open_trade["exch_seg"]

    exit_price = entry
    if token and exch_seg:
        try:
            broker = conn.execute(
                "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
                (user["id"],)
            ).fetchone()
            if broker:
                creds = {
                    "api_key": decrypt_credential(broker["api_key"]),
                    "client_id": broker["client_id"],
                    "password": decrypt_credential(broker["api_secret"]),
                    "totp_secret": decrypt_credential(broker["totp_secret"]) if broker["totp_secret"] else None,
                }
                obj = angel_login(creds)
                quote = obj.ltpData(exch_seg, symbol, token)
                exit_price = float(quote["data"]["ltp"])
        except Exception:
            pass

    pnl = round((exit_price - entry) * qty, 2)

    conn.execute(
        """UPDATE paper_trades
           SET exit_price=?, pnl=?, status='CLOSED', reason=?
           WHERE id=?""",
        (exit_price, pnl, "HERO ZERO FORCE EXIT (real premium)", open_trade["id"])
    )

    add_pnl(conn, user["id"], pnl)
    conn.commit()
    conn.close()

    try:
        msg = "\n".join([
            "📤 <b>Hero Zero Exit</b>",
            f"Symbol: {open_trade['symbol']}",
            f"Entry: Rs {entry}",
            f"Exit: Rs {exit_price}",
            f"Qty: {qty}",
            f"P&L: Rs {pnl}",
        ])
        notify_user(user["id"], msg)
    except Exception:
        pass

    return {
        "success": True,
        "message": "Hero Zero trade closed",
        "exit_price": exit_price,
        "pnl": pnl
    }
@router.post("/paper/clear-history")
def clear_paper_history(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    ensure_tables(conn)

    conn.execute(
        "DELETE FROM paper_trades WHERE user_id=?",
        (user["id"],)
    )

    save_bot_status(conn, user["id"], 0, "PAPER_HISTORY_CLEARED")

    conn.commit()
    conn.close()

    try:
        notify_user(user["id"], "🧹 <b>Paper trade history cleared</b>")
    except Exception:
        pass

    return {
        "success": True,
        "message": "Paper trade history cleared. Telegram settings safe."
    }



@router.get("/signal-history")
def get_signal_history(authorization: str = Header(None), limit: int = 100):
    """
    Returns recent real signal snapshots (price, score, adx, volume_ratio)
    for charting. Only populated when the real TQU engine has run
    (i.e. broker connected and bot started).
    """
    user = get_current_user(authorization)
    conn = get_db()
    ensure_tables(conn)

    rows = conn.execute(
        """SELECT price, score, signal, adx, volume_ratio, created_at
           FROM signal_history
           WHERE user_id=?
           ORDER BY id DESC LIMIT ?""",
        (user["id"], limit)
    ).fetchall()
    conn.close()

    points = [
        {
            "price": r["price"],
            "score": r["score"],
            "signal": r["signal"],
            "adx": r["adx"],
            "volume_ratio": r["volume_ratio"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    points.reverse()  # oldest first, best for charts

    return {
        "success": True,
        "count": len(points),
        "points": points,
    }
