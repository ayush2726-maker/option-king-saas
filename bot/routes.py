from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from auth.utils import decrypt_credential
from bot.strategy import is_hero_window_active, get_full_signal
from strategy.profile_engine import get_active_profile_config
from bot.history_provider import get_historical_rows
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
    "sl_percent": 0,
    "target_percent": 0,
    "trailing_sl": True,
    "exit_model": "DYNAMIC_ATR_PROFIT_LOCK",
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


def _persisted_bot_should_run(
    conn,
    user_id: int,
) -> bool:
    try:
        row = conn.execute(
            "SELECT is_running FROM user_bot_state WHERE user_id=?",
            (user_id,),
        ).fetchone()

        if row is not None:
            return bool(row["is_running"])
    except Exception:
        pass

    try:
        row = conn.execute(
            "SELECT is_running FROM bot_status WHERE user_id=?",
            (user_id,),
        ).fetchone()

        return (
            bool(row["is_running"])
            if row is not None
            else False
        )
    except Exception:
        return False


def _start_saved_runtime_engine(
    user_id: int,
):
    # Recreate in-memory broker engine after Railway restart.
    current = get_user_bot_state(user_id)

    if current.get("running"):
        return {
            "state": current,
            "started": False,
            "reason": None,
        }

    conn = get_db()

    try:
        ensure_tables(conn)

        if not _persisted_bot_should_run(
            conn,
            user_id,
        ):
            return {
                "state": current,
                "started": False,
                "reason": "BOT_STOPPED",
            }

        broker = conn.execute(
            "SELECT * FROM broker_credentials "
            "WHERE user_id=? AND is_active=1 "
            "ORDER BY last_connected DESC LIMIT 1",
            (user_id,),
        ).fetchone()

        if not broker:
            return {
                "state": current,
                "started": False,
                "reason": "BROKER_NOT_CONNECTED",
            }

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

        broker_name = str(
            broker["broker_name"]
            or "angelone"
        ).lower()

    except Exception as exc:
        return {
            "state": current,
            "started": False,
            "reason": (
                "CREDENTIAL_LOAD_FAILED: "
                + str(exc)[:120]
            ),
        }

    finally:
        conn.close()

    try:
        if broker_name == "angelone":
            result = start_user_bot(
                user_id,
                creds,
            )
        else:
            from bot.angel_fetcher import (
                start_user_bot_multi,
            )

            result = start_user_bot_multi(
                user_id,
                broker_name,
                creds,
            )

        recovered = get_user_bot_state(
            user_id
        )

        successful = bool(
            recovered.get("running")
        ) or bool(
            isinstance(result, dict)
            and result.get("success")
        )

        return {
            "state": recovered,
            "started": successful,
            "reason": (
                None
                if successful
                else (
                    result.get("message")
                    if isinstance(result, dict)
                    else "ENGINE_START_FAILED"
                )
            ),
        }

    except Exception as exc:
        return {
            "state": get_user_bot_state(
                user_id
            ),
            "started": False,
            "reason": (
                "ENGINE_START_FAILED: "
                + str(exc)[:120]
            ),
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

    for col, coltype in [
        ("sl_price","REAL"), ("target_price","REAL"),
        ("token","TEXT"), ("exch_seg","TEXT"),
        ("expiry","TEXT"), ("strike","REAL"),
        ("initial_risk","REAL"), ("peak_price","REAL"),
        ("trail_stage","TEXT"),
        ("trail_updates","INTEGER DEFAULT 0"),
        ("last_ltp","REAL"), ("broker_name","TEXT"),
        ("reversal_count","INTEGER DEFAULT 0"),
        ("reversal_last_candle","TEXT"),
        ("underlying","TEXT"),
        ("trading_mode","TEXT DEFAULT 'paper'"),
        ("capital_slot","INTEGER"),
        ("allocation_pct","REAL"),
        ("capital_base","REAL"),
        ("lot_size","INTEGER"),
        ("lots","INTEGER"),
        ("capital_used","REAL"),
        ("entry_order_id","TEXT"),
        ("exit_order_id","TEXT"),
        ("live_order_status","TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {coltype}")
        except Exception:
            pass

    conn.commit()


def _snapshot_number(value, default=0.0):
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
        # AUTO portfolio quantities are position-specific.
        # Never overwrite them with the chart instrument lot size.

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

    # Dynamic signal - REAL ENGINE (TQU strategy.py).
    # Restore the in-memory engine automatically after
    # a Railway deploy/restart when DB status is running.
    if is_running:
        engine_state = get_user_bot_state(
            user["id"]
        )

        if not engine_state.get("running"):
            recovery = _start_saved_runtime_engine(
                user["id"]
            )
            engine_state = recovery["state"]

        engine_ready = bool(
            engine_state.get("running")
        ) and (
            engine_state.get("strategy")
            in (
                "TQU_ENHANCED",
                "CUSTOM_PROFILE_V1",
            )
            or engine_state.get("engine_mode")
            == "AUTO_PORTFOLIO_V1"
        )

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
            selected_underlying = (
                engine_state.get("underlying")
                or primary
            )
            primary = selected_underlying
            entry_threshold = int(
                engine_state.get(
                    "min_score",
                    entry_threshold,
                )
            )
            if side in ("CE", "PE"):
                display_symbol, broker_symbol, strike, expiry = make_paper_option_symbol(
                    selected_underlying,
                    side,
                )
                symbol = display_symbol
            else:
                symbol = None

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
            _broker_check = conn.execute(
                "SELECT id FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
                (user["id"],)
            ).fetchone()
            status = "ENGINE_WARMING_UP" if _broker_check else "CONNECT_BROKER_FOR_REAL_SIGNAL"
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
    active_trades = []
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
            qty_v = int(t["qty"] or 0)

            trade_text = (str(t["symbol"] or "") + " " + str(t["reason"] or "")).upper()
            local_sl_percent = sl_percent
            local_target_percent = target_percent

            if "HEROZERO" in trade_text or "HERO ZERO" in trade_text:
                local_sl_percent = 50
                local_target_percent = 100

            sl_price = (
                round(float(t["sl_price"]), 2)
                if t["sl_price"] is not None
                else None
            )
            target_price = None

            current_price = None
            unrealized_pnl = None

            if str(t["status"]).upper() == "OPEN":
                current_price = (
                    round(float(t["last_ltp"]), 2)
                    if (
                        "last_ltp" in t.keys()
                        and t["last_ltp"] is not None
                    )
                    else entry
                )
                unrealized_pnl = round(
                    (current_price - entry) * qty_v,
                    2,
                )

            return {
                "id": t["id"],
                "symbol": t["symbol"],
                "side": t["side"],
                "qty": qty_v,
                "underlying": (
                    t["underlying"]
                    if (
                        "underlying" in t.keys()
                        and t["underlying"]
                    )
                    else primary
                ),
                "trading_mode": (
                    t["trading_mode"]
                    if (
                        "trading_mode" in t.keys()
                        and t["trading_mode"]
                    )
                    else "paper"
                ),
                "capital_slot": (
                    int(t["capital_slot"])
                    if (
                        "capital_slot" in t.keys()
                        and t["capital_slot"] is not None
                    )
                    else None
                ),
                "allocation_pct": (
                    float(t["allocation_pct"])
                    if (
                        "allocation_pct" in t.keys()
                        and t["allocation_pct"] is not None
                    )
                    else None
                ),
                "lot_size": (
                    int(t["lot_size"])
                    if (
                        "lot_size" in t.keys()
                        and t["lot_size"] is not None
                    )
                    else None
                ),
                "lots": (
                    int(t["lots"])
                    if (
                        "lots" in t.keys()
                        and t["lots"] is not None
                    )
                    else None
                ),
                "capital_used": (
                    float(t["capital_used"])
                    if (
                        "capital_used" in t.keys()
                        and t["capital_used"] is not None
                    )
                    else None
                ),
                "entry_price": entry,
                "current_price": current_price,
                "sl_price": sl_price,
                "target_price": target_price,
                "reversal_count": int(t["reversal_count"] or 0),
                "reversal_last_candle": t["reversal_last_candle"],
                "exit_price": float(exit_p) if exit_p is not None else None,
                "pnl": float(t["pnl"] or 0),
                "unrealized_pnl": unrealized_pnl,
                "status": t["status"],
                "reason": t["reason"],
                "created_at": t["created_at"],
            }

        open_rows = conn.execute(
            """SELECT * FROM paper_trades
               WHERE user_id=? AND status='OPEN'
               ORDER BY capital_slot ASC, id ASC""",
            (user["id"],)
        ).fetchall()
        active_trades = [
            make_trade_view(row)
            for row in open_rows
        ]
        active_trade = (
            active_trades[0]
            if active_trades
            else None
        )
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
        "active_trades": active_trades,
        "open_trade_count": len(active_trades),
        "latest_trade": latest_trade,
        "engine_mode": (
            engine_state.get("engine_mode")
            if is_running
            else None
        ),
        "scan_results": (
            engine_state.get("scan_results", [])
            if is_running
            else []
        ),
        "capital_plan": (
            engine_state.get(
                "capital_plan",
                {
                    "slot_1_percent": 50,
                    "slot_2_percent": 40,
                    "reserve_percent": 10,
                    "max_open_positions": 2,
                    "different_index_required": True,
                },
            )
            if is_running
            else {
                "slot_1_percent": 50,
                "slot_2_percent": 40,
                "reserve_percent": 10,
                "max_open_positions": 2,
                "different_index_required": True,
            }
        ),
        "strategy_profile_name": (
            engine_state.get(
                "strategy_profile_name",
                "OKAI Default 82",
            )
            if is_running
            else "OKAI Default 82"
        ),
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
                bname = broker["broker_name"]
                if bname == "angelone":
                    start_user_bot(user["id"], creds)
                else:
                    from bot.angel_fetcher import start_user_bot_multi
                    start_user_bot_multi(user["id"], bname, creds)
                engine_note = f"Real TQU signal engine started via {bname} (paper mode - real orders OFF)."
            except Exception as e:
                print(f"[bot/start] broker engine error: {e}")

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

    if broker["broker_name"] == "angelone":
        res = start_user_bot(user["id"], creds)
    else:
        from bot.angel_fetcher import start_user_bot_multi
        res = start_user_bot_multi(user["id"], broker["broker_name"], creds)
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



def _chart_range_days(value):
    try:
        days = int(value or 1)
    except Exception:
        days = 1
    return days if days in (1, 7, 30) else 1


def _historical_indicator_candles(rows, profile=None):
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

def _fetch_broker_historical_candles(user_id, instrument, days):
    rows, reason, broker_name, cached = get_historical_rows(
        user_id,
        instrument,
        days,
    )
    interval = "ONE_MINUTE" if int(days) == 1 else "FIFTEEN_MINUTE" if int(days) == 7 else "ONE_HOUR"
    try:
        active_profile = get_active_profile_config(user_id)
    except Exception:
        active_profile = None
    candles = _historical_indicator_candles(rows, profile=active_profile)
    if rows and not candles:
        reason = "Broker candles mili, lekin chart format prepare nahi hua."
    return candles, reason, interval, broker_name, cached


@router.get("/chart-data")
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
    history_broker = None
    history_cached = False

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
        source = "BROKER_HISTORICAL"
        history_reason = None
        try:
            candles, history_reason, interval, history_broker, history_cached = _fetch_broker_historical_candles(
                user["id"], requested_instrument, requested_days
            )
            if history_broker:
                source = f"{history_broker.upper()}_HISTORICAL"
            if history_cached:
                source += "_CACHE"
        except Exception:
            history_reason = "Historical graph load nahi hua. 45 seconds baad pull-down refresh karein."

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
        "broker": history_broker,
        "cached": history_cached,
        "count": len(candles),
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
        "updated_at": state.get("updated_at") or datetime.utcnow().isoformat(),
    }


@router.get("/signal-history")
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

