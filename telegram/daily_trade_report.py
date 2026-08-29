"""Send one end-of-session Telegram trade report per connected user."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import threading
import time as time_module

from database import get_db
from telegram.daily_report_format import build_daily_trade_report
from telegram.routes import notify_trade_alert


IST = timezone(timedelta(hours=5, minutes=30))
REPORT_AT_IST = time(15, 45)
_started = False
_start_lock = threading.Lock()


def _utc_day_window(now_utc):
    current_ist = now_utc.astimezone(IST)
    start_ist = datetime.combine(current_ist.date(), time.min, tzinfo=IST)
    start_utc = start_ist.astimezone(timezone.utc)
    return current_ist, start_utc, start_utc + timedelta(days=1)


def _sql_time(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (str(table),),
    ).fetchone()
    return bool(row)


def _market_session_evidence(conn, start_utc, end_utc):
    start = _sql_time(start_utc)
    end = _sql_time(end_utc)
    for table in ("signal_history", "paper_trades", "trades"):
        if not _table_exists(conn, table):
            continue
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE datetime(created_at)>=datetime(?) "
            "AND datetime(created_at)<datetime(?) LIMIT 1",
            (start, end),
        ).fetchone()
        if row:
            return True
    return False


def _daily_rows(conn, table, user_id, start_utc, end_utc):
    if not _table_exists(conn, table):
        return []
    return conn.execute(
        f"SELECT * FROM {table} WHERE user_id=? "
        "AND datetime(created_at)>=datetime(?) AND datetime(created_at)<datetime(?) "
        "ORDER BY id ASC",
        (int(user_id), _sql_time(start_utc), _sql_time(end_utc)),
    ).fetchall()


def _ensure_report_log(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_daily_report_log (
            user_id INTEGER NOT NULL,
            report_date TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            PRIMARY KEY(user_id, report_date)
        )
        """
    )
    conn.commit()


def send_daily_trade_reports_once(now_utc=None):
    current_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_ist, start_utc, end_utc = _utc_day_window(current_utc)
    if current_ist.weekday() >= 5 or current_ist.time() < REPORT_AT_IST:
        return {"sent": 0, "skipped": "OUTSIDE_REPORT_WINDOW"}

    conn = get_db()
    sent = 0
    try:
        _ensure_report_log(conn)
        if not _market_session_evidence(conn, start_utc, end_utc):
            return {"sent": 0, "skipped": "NO_MARKET_SESSION_EVIDENCE"}
        if not _table_exists(conn, "telegram_settings"):
            return {"sent": 0, "skipped": "NO_TELEGRAM_SETTINGS"}

        users = conn.execute(
            """
            SELECT user_id FROM telegram_settings
            WHERE enabled=1 AND COALESCE(chat_id, '')<>''
              AND COALESCE(send_trade_alerts, 1)=1
            ORDER BY user_id ASC
            """
        ).fetchall()
        report_date = current_ist.date().isoformat()
        for user in users:
            user_id = int(user["user_id"])
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO telegram_daily_report_log(
                    user_id, report_date, status, created_at
                ) VALUES (?, ?, 'sending', ?)
                """,
                (user_id, report_date, current_utc.isoformat()),
            )
            conn.commit()
            if not inserted.rowcount:
                continue

            paper_rows = _daily_rows(conn, "paper_trades", user_id, start_utc, end_utc)
            live_rows = _daily_rows(conn, "trades", user_id, start_utc, end_utc)
            message = build_daily_trade_report(paper_rows, live_rows, current_ist.date())
            result = notify_trade_alert(user_id, message)
            if result.get("success"):
                conn.execute(
                    """
                    UPDATE telegram_daily_report_log
                    SET status='sent', sent_at=? WHERE user_id=? AND report_date=?
                    """,
                    (datetime.now(timezone.utc).isoformat(), user_id, report_date),
                )
                conn.commit()
                sent += 1
            else:
                conn.execute(
                    "DELETE FROM telegram_daily_report_log WHERE user_id=? AND report_date=?",
                    (user_id, report_date),
                )
                conn.commit()
        return {"sent": sent, "date": report_date}
    finally:
        conn.close()


def schedule_daily_trade_reports():
    global _started
    with _start_lock:
        if _started:
            return False
        _started = True

    def worker():
        while True:
            try:
                send_daily_trade_reports_once()
            except Exception as exc:
                print(f"Telegram daily trade report warning: {str(exc)[:180]}")
            time_module.sleep(30)

    threading.Thread(
        target=worker,
        name="okai-telegram-daily-trade-report",
        daemon=True,
    ).start()
    return True


__all__ = [
    "REPORT_AT_IST",
    "schedule_daily_trade_reports",
    "send_daily_trade_reports_once",
]
