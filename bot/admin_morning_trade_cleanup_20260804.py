"""Permanently remove the admin's invalid opening-session PAPER trades.

User-requested one-time cleanup:

* Entry date in IST: 04 Aug 2026
* Entry time in IST: 09:15:00 through 09:38:59 inclusive
* PAPER rows only
* All indices/sides/statuses in that exact window

The rows are permanently deleted, not archived. Related cooldown/re-entry/order
state is cleared and stored bot totals are rebuilt from the remaining trades.
No row before/after the exact window and no LIVE trade is touched.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from database import get_db


VERSION = "ADMIN_PAPER_DELETE_20260804_0915_0938_V1"
IST = ZoneInfo("Asia/Kolkata")
TARGET_DATE = date(2026, 8, 4)
START_TIME = time(9, 15, 0)
END_TIME = time(9, 38, 59, 999999)


def _keys(row: Any) -> set[str]:
    try:
        return set(row.keys())
    except Exception:
        return set()


def _value(row: Any, key: str, default: Any = None) -> Any:
    try:
        if key in _keys(row) and row[key] is not None:
            return row[key]
    except Exception:
        pass
    if isinstance(row, dict):
        value = row.get(key)
        return default if value is None else value
    return default


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if " " in text and "T" not in text:
            text = text.replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except Exception:
            return None

    # Existing Option King timestamps without an offset are stored as UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(IST)


def _entry_time_ist(row: Any) -> Optional[datetime]:
    return _parse_datetime(
        _value(row, "entry_time")
        or _value(row, "created_at")
        or _value(row, "timestamp")
    )


def _is_paper(row: Any) -> bool:
    return str(_value(row, "trading_mode", "paper") or "paper").lower() != "live"


def _matches_target(row: Any) -> bool:
    if not _is_paper(row):
        return False
    entered = _entry_time_ist(row)
    if entered is None or entered.date() != TARGET_DATE:
        return False
    entered_time = entered.timetz().replace(tzinfo=None)
    return START_TIME <= entered_time <= END_TIME


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table,),
            ).fetchone()
        )
    except Exception:
        return False


def _columns(conn, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _target_user_ids(conn) -> list[int]:
    user_columns = _columns(conn, "users")
    if not user_columns:
        return []

    admin_email = str(os.getenv("ADMIN_EMAIL") or "").strip()
    rows = []
    if admin_email and "email" in user_columns:
        rows = conn.execute(
            "SELECT id FROM users WHERE lower(email)=lower(?)",
            (admin_email,),
        ).fetchall()

    if not rows and "is_admin" in user_columns:
        rows = conn.execute(
            "SELECT id FROM users WHERE COALESCE(is_admin, 0)=1 ORDER BY id ASC"
        ).fetchall()

    output: list[int] = []
    for row in rows:
        try:
            output.append(int(row["id"]))
        except Exception:
            try:
                output.append(int(row[0]))
            except Exception:
                continue
    return sorted(set(output))


def _ensure_run_log(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_run_log (
            version TEXT PRIMARY KEY,
            ran_at TEXT NOT NULL,
            affected_users INTEGER NOT NULL DEFAULT 0,
            removed_rows INTEGER NOT NULL DEFAULT 0,
            removed_recorded_pnl REAL NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.commit()


def _already_applied(conn) -> bool:
    _ensure_run_log(conn)
    return bool(
        conn.execute(
            "SELECT 1 FROM maintenance_run_log WHERE version=? LIMIT 1",
            (VERSION,),
        ).fetchone()
    )


def _delete_related_state(conn, trade_id: int, user_id: int) -> None:
    trade_id_statements = (
        ("auto_reentry_blocks", "source_trade_id"),
        ("auto_user_cooldowns", "source_trade_id"),
        ("live_order_events", "trade_id"),
        ("paper_trade_events", "trade_id"),
        ("trade_events", "trade_id"),
    )
    for table, column in trade_id_statements:
        if table not in {"paper_trades", "users"} and column in _columns(conn, table):
            try:
                conn.execute(f"DELETE FROM {table} WHERE {column}=?", (trade_id,))
            except Exception:
                pass

    # The deleted losses must not keep a stale cooldown/re-entry block active.
    for table in ("auto_user_cooldowns", "auto_reentry_blocks"):
        if "user_id" in _columns(conn, table):
            try:
                conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
            except Exception:
                pass


def _closed_pnl_expression(columns: set[str]) -> str:
    if "net_pnl" in columns and "pnl" in columns:
        return "COALESCE(net_pnl, pnl, 0)"
    if "net_pnl" in columns:
        return "COALESCE(net_pnl, 0)"
    if "pnl" in columns:
        return "COALESCE(pnl, 0)"
    return "0"


def _rebuild_user_totals(conn, user_id: int) -> dict[str, float | int]:
    trade_columns = _columns(conn, "paper_trades")
    if not trade_columns:
        return {"total_trades": 0, "total_pnl": 0.0}

    total_trades = int(
        conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE user_id=?",
            (user_id,),
        ).fetchone()[0]
    )
    pnl_expression = _closed_pnl_expression(trade_columns)
    if "status" in trade_columns:
        total_pnl = float(
            conn.execute(
                f"""
                SELECT COALESCE(SUM({pnl_expression}), 0)
                FROM paper_trades
                WHERE user_id=? AND UPPER(COALESCE(status, '')) <> 'OPEN'
                """,
                (user_id,),
            ).fetchone()[0]
            or 0
        )
    else:
        total_pnl = float(
            conn.execute(
                f"SELECT COALESCE(SUM({pnl_expression}), 0) FROM paper_trades WHERE user_id=?",
                (user_id,),
            ).fetchone()[0]
            or 0
        )

    status_columns = _columns(conn, "bot_status")
    assignments = []
    values: list[Any] = []
    if "total_trades" in status_columns:
        assignments.append("total_trades=?")
        values.append(total_trades)
    if "total_pnl" in status_columns:
        assignments.append("total_pnl=?")
        values.append(round(total_pnl, 2))
    if "updated_at" in status_columns:
        assignments.append("updated_at=?")
        values.append(datetime.now(timezone.utc).isoformat())
    if assignments and "user_id" in status_columns:
        values.append(user_id)
        conn.execute(
            f"UPDATE bot_status SET {', '.join(assignments)} WHERE user_id=?",
            tuple(values),
        )

    return {
        "total_trades": total_trades,
        "total_pnl": round(total_pnl, 2),
    }


def delete_admin_morning_paper_trades_20260804() -> dict[str, Any]:
    """Permanently delete the exact user-requested trade window once."""
    conn = get_db()
    try:
        if _already_applied(conn):
            return {
                "already_applied": True,
                "removed": 0,
                "affected_users": 0,
                "version": VERSION,
            }

        target_users = _target_user_ids(conn)
        if not target_users:
            # Do not mark complete if the configured/admin user is not available yet.
            return {
                "already_applied": False,
                "removed": 0,
                "affected_users": 0,
                "reason": "ADMIN_USER_NOT_FOUND",
                "version": VERSION,
            }

        placeholders = ",".join("?" for _ in target_users)
        rows = conn.execute(
            f"SELECT * FROM paper_trades WHERE user_id IN ({placeholders}) ORDER BY id ASC",
            tuple(target_users),
        ).fetchall()

        removed = 0
        removed_recorded_pnl = 0.0
        removed_ids: list[int] = []
        affected_users: set[int] = set()

        for row in rows:
            if not _matches_target(row):
                continue
            trade_id = int(_value(row, "id", 0) or 0)
            user_id = int(_value(row, "user_id", 0) or 0)
            if trade_id <= 0 or user_id <= 0:
                continue

            pnl_value = _value(row, "net_pnl")
            if pnl_value is None:
                pnl_value = _value(row, "pnl", 0)
            try:
                removed_recorded_pnl += float(pnl_value or 0)
            except Exception:
                pass

            _delete_related_state(conn, trade_id, user_id)
            conn.execute("DELETE FROM paper_trades WHERE id=?", (trade_id,))
            removed += 1
            removed_ids.append(trade_id)
            affected_users.add(user_id)

        rebuilt = {
            str(user_id): _rebuild_user_totals(conn, user_id)
            for user_id in affected_users
        }

        details = {
            "target_date_ist": TARGET_DATE.isoformat(),
            "window_ist": "09:15:00-09:38:59 inclusive",
            "paper_only": True,
            "removed_trade_ids": removed_ids,
            "rebuilt_totals": rebuilt,
        }
        conn.execute(
            """
            INSERT INTO maintenance_run_log (
                version, ran_at, affected_users, removed_rows,
                removed_recorded_pnl, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                VERSION,
                datetime.now(timezone.utc).isoformat(),
                len(affected_users),
                removed,
                round(removed_recorded_pnl, 2),
                json.dumps(details, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()

        return {
            "already_applied": False,
            "removed": removed,
            "affected_users": len(affected_users),
            "removed_recorded_pnl": round(removed_recorded_pnl, 2),
            "removed_trade_ids": removed_ids,
            "rebuilt_totals": rebuilt,
            "version": VERSION,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
