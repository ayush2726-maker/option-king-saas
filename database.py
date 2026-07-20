import sqlite3
import os
import shutil
from pathlib import Path
from datetime import datetime


DB_FILENAME = "option_king_saas.db"


def _resolve_database_path():
    explicit = str(
        os.getenv("DB_PATH", "")
    ).strip()
    mount = str(
        os.getenv(
            "RAILWAY_VOLUME_MOUNT_PATH",
            "",
        )
    ).strip()

    if explicit:
        db_file = Path(explicit).expanduser()
        source = "DB_PATH"
    elif mount:
        db_file = Path(mount) / DB_FILENAME
        source = "RAILWAY_VOLUME_MOUNT_PATH"
    else:
        db_file = Path(DB_FILENAME)
        source = "LOCAL_EPHEMERAL"

    db_file = db_file.resolve()
    db_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    volume_path = (
        Path(mount).resolve()
        if mount
        else None
    )

    return db_file, source, volume_path


_DB_FILE, DB_STORAGE_SOURCE, _VOLUME_PATH = (
    _resolve_database_path()
)

DB_PATH = str(_DB_FILE)


def _inside_volume(child, parent):
    if parent is None:
        return False

    try:
        child.relative_to(parent)
        return True
    except Exception:
        return False


DB_STORAGE_PERSISTENT = _inside_volume(
    _DB_FILE,
    _VOLUME_PATH,
)


def _copy_local_database_once():
    local_file = Path(
        DB_FILENAME
    ).resolve()

    if (
        local_file == _DB_FILE
        or _DB_FILE.exists()
        or not local_file.exists()
        or local_file.stat().st_size <= 0
    ):
        return

    try:
        shutil.copy2(
            local_file,
            _DB_FILE,
        )
        print(
            "✅ Existing database copied to "
            f"persistent path: {DB_PATH}"
        )
    except Exception as exc:
        print(
            "⚠️ Database copy skipped: "
            f"{str(exc)[:160]}"
        )


_copy_local_database_once()


def get_db_storage_info():
    return {
        "path": DB_PATH,
        "source": DB_STORAGE_SOURCE,
        "persistent": bool(
            DB_STORAGE_PERSISTENT
        ),
        "volume_attached": bool(
            _VOLUME_PATH
        ),
        "exists": _DB_FILE.exists(),
        "size_bytes": (
            _DB_FILE.stat().st_size
            if _DB_FILE.exists()
            else 0
        ),
    }


def get_db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(
        "PRAGMA journal_mode=WAL"
    )
    conn.execute(
        "PRAGMA synchronous=NORMAL"
    )
    conn.execute(
        "PRAGMA busy_timeout=10000"
    )
    conn.execute(
        "PRAGMA foreign_keys=ON"
    )
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    # Users table
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            phone TEXT,
            is_active INTEGER DEFAULT 1,
            is_admin INTEGER DEFAULT 0,
            trial_ends_at TEXT,
            subscription_status TEXT DEFAULT 'trial',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Registration risk/terms consent audit
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            policy_version TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            accepted_text TEXT NOT NULL,
            age_confirmed INTEGER NOT NULL DEFAULT 0,
            risk_acknowledged INTEGER NOT NULL DEFAULT 0,
            no_guarantee_acknowledged INTEGER NOT NULL DEFAULT 0,
            technology_risk_acknowledged INTEGER NOT NULL DEFAULT 0,
            terms_accepted INTEGER NOT NULL DEFAULT 0,
            privacy_accepted INTEGER NOT NULL DEFAULT 0,
            algo_order_authorized INTEGER NOT NULL DEFAULT 0,
            whatsapp_trade_alert_opt_in INTEGER NOT NULL DEFAULT 0,
            accepted_at TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, policy_version),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_consents_user
        ON user_consents(user_id)
    """)

    # Broker credentials table (encrypted)
    c.execute("""
        CREATE TABLE IF NOT EXISTS broker_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            broker_name TEXT NOT NULL,
            client_id TEXT NOT NULL,
            api_key TEXT NOT NULL,
            api_secret TEXT NOT NULL,
            totp_secret TEXT,
            is_active INTEGER DEFAULT 1,
            last_connected TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Subscriptions table
    c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            amount REAL NOT NULL,
            razorpay_order_id TEXT,
            razorpay_payment_id TEXT,
            status TEXT DEFAULT 'pending',
            valid_from TEXT,
            valid_till TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Trades table (per user)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            broker TEXT,
            symbol TEXT,
            option_type TEXT,
            entry_price REAL,
            exit_price REAL,
            quantity INTEGER,
            pnl REAL,
            status TEXT DEFAULT 'open',
            entry_time TEXT,
            exit_time TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Bot status table
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            is_running INTEGER DEFAULT 0,
            last_signal TEXT,
            last_trade_at TEXT,
            total_trades INTEGER DEFAULT 0,
            total_pnl REAL DEFAULT 0.0,
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)


    # Strategy settings table
    c.execute("""
        CREATE TABLE IF NOT EXISTS strategy_settings (
            user_id INTEGER PRIMARY KEY,
            settings_json TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Backtest runs table
    c.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            instrument TEXT,
            test_date TEXT,
            settings_json TEXT,
            summary_json TEXT,
            trades_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)


    # Telegram settings table
    c.execute("""
        CREATE TABLE IF NOT EXISTS telegram_settings (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER DEFAULT 0,
            bot_token TEXT,
            chat_id TEXT,
            send_bot_alerts INTEGER DEFAULT 1,
            send_trade_alerts INTEGER DEFAULT 1,
            send_backtest_alerts INTEGER DEFAULT 1,
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()
    storage = get_db_storage_info()
    storage_type = (
        "PERSISTENT"
        if storage["persistent"]
        else "EPHEMERAL"
    )

    print(
        "✅ Database initialized successfully "
        f"| {storage_type} "
        f"| {storage['path']}"
    )

    if (
        os.getenv("RAILWAY_ENVIRONMENT_ID")
        and not storage["persistent"]
    ):
        print(
            "⚠️ Railway persistent volume "
            "attach nahi hai."
        )

if __name__ == "__main__":
    init_db()

def init_bot_status_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_status (
            id INTEGER PRIMARY KEY,
            is_running INTEGER DEFAULT 0,
            started_at TEXT
        )
    """)
    conn.commit()
    conn.close()
