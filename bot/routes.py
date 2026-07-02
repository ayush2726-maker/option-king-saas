from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from bot.strategy import is_hero_window_active
from telegram.routes import notify_user
from datetime import datetime
import json
import random

try:
    from bot.angel_fetcher import start_user_bot, stop_user_bot
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

    conn.commit()


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

        random.seed(f"exit-{user['id']}-{open_trade['id']}-{now.strftime('%H%M%S')}")
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

        if move_pct * 100 >= target_percent:
            exit_reason = f"TARGET HIT {round(move_pct * 100, 2)}%"
        elif move_pct * 100 <= -sl_percent:
            exit_reason = f"SL HIT {round(move_pct * 100, 2)}%"
        elif age_min >= 10:
            exit_reason = f"TIME EXIT {round(move_pct * 100, 2)}%"

        if exit_reason:
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

    # Dynamic paper signal
    if trading_mode == "paper" and is_running:
        random.seed(f"{user['id']}-{primary}-{now.strftime('%H%M%S')}")

        adx = round(random.uniform(18, 42), 1)
        volume_ratio = round(random.uniform(0.75, 2.40), 2)
        mtf_ok = random.choice([True, True, True, False])

        base_score = random.choice([30, 35, 40])
        adx_score = int(settings.get("adx_score", 20)) if adx >= adx_threshold else 0
        volume_score = int(settings.get("volume_score", 20)) if volume_ratio >= volume_threshold else 0
        mtf_score = int(settings.get("mtf_score", 10)) if mtf_ok else 0
        regime_score = int(settings.get("regime_score", 10)) if random.random() > 0.25 else 0

        score = min(100, base_score + adx_score + volume_score + mtf_score + regime_score)

        side = "CE" if random.random() > 0.5 else "PE"
        display_symbol, broker_symbol, strike, expiry = make_paper_option_symbol(primary, side)
        symbol = display_symbol
        signal = "BUY_" + side if score >= entry_threshold else "PAPER_WAITING"
        status = "PAPER_RUNNING"

        open_trade = conn.execute(
            """SELECT id FROM paper_trades
               WHERE user_id=? AND status='OPEN'
               ORDER BY id DESC LIMIT 1""",
            (user["id"],)
        ).fetchone()

        if False and score >= entry_threshold and not open_trade:
            entry_price = round(random.uniform(90, 180), 2)

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
                    now.isoformat()
                )
            )

            add_trade_count(conn, user["id"], signal)

            try:
                msg = "\n".join([
                    "📝 <b>Paper Trade Entry</b>",
                    f"Symbol: {symbol}",
                    f"Side: {side}",
                    f"Qty: {qty}",
                    f"Entry: ₹{entry_price}",
                    f"Score: {score}/{entry_threshold}",
                ])
                notify_user(user["id"], msg)
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
        base_score = 0
        adx_score = 0
        volume_score = 0
        mtf_score = 0
        regime_score = 0

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

            sl_price = round(entry * (1 - sl_percent / 100), 2) if entry else None
            target_price = round(entry * (1 + target_percent / 100), 2) if entry else None

            current_price = None
            unrealized_pnl = None

            if str(t["status"]).upper() == "OPEN":
                random.seed(f"view-{user['id']}-{t['id']}-{datetime.utcnow().strftime('%H%M%S')}")
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
        "message": "Paper score demo only. Real option chain feed not connected yet."
    }


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
        conn.close()

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
            "message": "Paper mode bot started. Real orders OFF.",
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
        "api_key": broker["api_key"],
        "client_id": broker["client_id"],
        "password": broker["api_secret"],
        "totp_secret": broker["totp_secret"],
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
