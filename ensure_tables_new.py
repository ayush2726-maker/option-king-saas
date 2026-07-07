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

