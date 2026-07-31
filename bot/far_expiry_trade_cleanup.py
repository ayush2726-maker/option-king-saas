"""Archive and remove invalid far-expiry PAPER trades for every user.

The cleanup is intentionally limited to PAPER rows. LIVE rows are retained because
broker executions are real records and must never be silently deleted from the app.

A normal contract is considered invalid when its expiry distance from the trade's
IST entry date exceeds the same final guard used by live execution:

* NIFTY: 8 calendar days
* SENSEX: 8 calendar days
* BANKNIFTY: 40 calendar days (monthly contracts)
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from database import get_db


VERSION = "OKAI-FAR-EXPIRY-PAPER-CLEANUP-V1"
IST = ZoneInfo("Asia/Kolkata")
MAX_DTE = {
    "NIFTY": 8,
    "SENSEX": 8,
    "BANKNIFTY": 40,
}
MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


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
    return default


def _underlying(row: Any) -> str:
    raw = str(_value(row, "underlying", "") or "").upper()
    compact = raw.replace(" ", "").replace("-", "")
    if compact in {"NIFTY", "NIFTY50"}:
        return "NIFTY"
    if compact in {"BANKNIFTY", "NIFTYBANK"}:
        return "BANKNIFTY"
    if "SENSEX" in compact:
        return "SENSEX"

    symbol = str(_value(row, "symbol", "") or "").upper().replace(" ", "")
    if "BANKNIFTY" in symbol or "NIFTYBANK" in symbol:
        return "BANKNIFTY"
    if "SENSEX" in symbol:
        return "SENSEX"
    if "NIFTY" in symbol:
        return "NIFTY"
    return ""


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

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _entry_day_ist(row: Any) -> Optional[date]:
    parsed = _parse_datetime(_value(row, "created_at"))
    return parsed.astimezone(IST).date() if parsed else None


def _parse_expiry_value(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    raw = str(value or "").strip()
    if not raw:
        return None

    for candidate in (raw[:10], raw):
        for fmt in (
            "%Y-%m-%d",
            "%d-%m-%Y",
            "%d%b%Y",
            "%d-%b-%Y",
            "%d %b %Y",
            "%d %b %y",
        ):
            try:
                return datetime.strptime(candidate.upper(), fmt).date()
            except (TypeError, ValueError):
                continue

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _expiry_from_symbol(symbol: Any) -> Optional[date]:
    raw = str(symbol or "").upper()
    if not raw:
        return None

    match4 = re.search(r"(\d{1,2})\s*([A-Z]{3})\s*(20\d{2})", raw)
    if match4:
        day, month_text, year = match4.groups()
        month = MONTHS.get(month_text)
        if month:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                return None

    # Broker symbols commonly concatenate expiry and strike, for example
    # SENSEX27AUG2678100PE. Exactly two digits after the month are the year.
    match2 = re.search(r"(\d{1,2})\s*([A-Z]{3})\s*(\d{2})", raw)
    if match2:
        day, month_text, year = match2.groups()
        month = MONTHS.get(month_text)
        if month:
            try:
                return date(2000 + int(year), month, int(day))
            except ValueError:
                return None
    return None


def _expiry(row: Any) -> Optional[date]:
    parsed = _parse_expiry_value(_value(row, "expiry"))
    if parsed:
        return parsed
    return _expiry_from_symbol(_value(row, "symbol", ""))


def _paper_mode(row: Any) -> bool:
    return str(_value(row, "trading_mode", "paper") or "paper").lower() != "live"


def _far_expiry(row: Any) -> Optional[dict]:
    if not _paper_mode(row):
        return None

    underlying = _underlying(row)
    if underlying not in MAX_DTE:
        return None

    entry_day = _entry_day_ist(row)
    expiry_day = _expiry(row)
    if entry_day is None or expiry_day is None:
        return None

    dte = (expiry_day - entry_day).days
    max_dte = MAX_DTE[underlying]
    if dte <= max_dte:
        return None

    return {
        "underlying": underlying,
        "entry_day_ist": entry_day.isoformat(),
        "expiry": expiry_day.isoformat(),
        "expiry_dte": dte,
        "max_expiry_dte": max_dte,
        "cleanup_reason": "INVALID_FAR_EXPIRY_PAPER_TRADE",
        "cleanup_version": VERSION,
    }


def _ensure_archive(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invalid_far_expiry_trades_archive (
            trade_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            underlying TEXT,
            entry_day_ist TEXT,
            expiry TEXT,
            expiry_dte INTEGER,
            max_expiry_dte INTEGER,
            original_status TEXT,
            original_pnl REAL,
            cleanup_reason TEXT NOT NULL,
            cleanup_version TEXT NOT NULL,
            archived_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _delete_related_state(conn, trade_id: int, user_id: int) -> None:
    statements = (
        ("DELETE FROM auto_reentry_blocks WHERE source_trade_id=?", (trade_id,)),
        ("DELETE FROM auto_user_cooldowns WHERE source_trade_id=?", (trade_id,)),
        ("DELETE FROM live_order_events WHERE trade_id=?", (trade_id,)),
    )
    for sql, params in statements:
        try:
            conn.execute(sql, params)
        except Exception:
            pass

    # Removing a historical bad trade changes the user's true loss streak, so any
    # cached cooldown for that user must be rebuilt from remaining valid trades.
    for sql in (
        "DELETE FROM auto_user_cooldowns WHERE user_id=?",
        "DELETE FROM auto_reentry_blocks WHERE user_id=?",
    ):
        try:
            conn.execute(sql, (user_id,))
        except Exception:
            pass


def cleanup_invalid_far_expiry_paper_trades() -> dict:
    """Archive and remove all historical/current invalid far-expiry PAPER rows."""
    conn = get_db()
    removed = 0
    removed_pnl = 0.0
    affected_users: set[int] = set()
    by_underlying = {name: 0 for name in MAX_DTE}

    try:
        _ensure_archive(conn)
        rows = conn.execute(
            "SELECT * FROM paper_trades ORDER BY id ASC"
        ).fetchall()

        for row in rows:
            check = _far_expiry(row)
            if not check:
                continue

            trade_id = int(_value(row, "id", 0) or 0)
            user_id = int(_value(row, "user_id", 0) or 0)
            if trade_id <= 0:
                continue

            payload = dict(row)
            pnl = float(_value(row, "pnl", 0) or 0)
            conn.execute(
                """
                INSERT OR IGNORE INTO invalid_far_expiry_trades_archive (
                    trade_id, user_id, underlying, entry_day_ist,
                    expiry, expiry_dte, max_expiry_dte,
                    original_status, original_pnl, cleanup_reason,
                    cleanup_version, archived_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    user_id,
                    check["underlying"],
                    check["entry_day_ist"],
                    check["expiry"],
                    check["expiry_dte"],
                    check["max_expiry_dte"],
                    str(_value(row, "status", "") or ""),
                    pnl,
                    check["cleanup_reason"],
                    VERSION,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )

            _delete_related_state(conn, trade_id, user_id)
            conn.execute("DELETE FROM paper_trades WHERE id=?", (trade_id,))

            removed += 1
            removed_pnl += pnl
            affected_users.add(user_id)
            by_underlying[check["underlying"]] += 1

        conn.commit()
        return {
            "removed": removed,
            "affected_users": len(affected_users),
            "removed_recorded_pnl": round(removed_pnl, 2),
            "by_underlying": by_underlying,
            "live_trades_removed": 0,
            "cleanup_version": VERSION,
        }
    finally:
        conn.close()
