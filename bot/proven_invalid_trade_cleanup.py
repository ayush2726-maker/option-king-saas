"""Archive and remove PAPER trades that have explicit proof they were invalid.

This cleanup is intentionally conservative.  It never guesses from present-day
settings and never deletes LIVE broker executions.  A PAPER row is removed only
when the row itself contains durable evidence that the final entry should have
been blocked, such as a saved score below its saved threshold, saved option
quality failure, an explicit block marker, impossible side/price/quantity, or an
expired contract at entry time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from database import get_db
from bot.far_expiry_trade_cleanup import (
    _entry_day_ist,
    _expiry,
    _paper_mode,
    _underlying,
    _value,
)


VERSION = "OKAI-PROVEN-INVALID-PAPER-CLEANUP-V1"

STRONG_BLOCK_MARKERS = (
    "TRADE_ALLOWED_FALSE",
    "ENTRY_SCORE_BELOW_THRESHOLD",
    "OPTION_ENTRY_QUALITY_BLOCKED",
    "OPTION_QUALITY_BLOCKED",
    "TWO_CONSECUTIVE_LOSSES_GLOBAL_COOLDOWN_15M",
    "POST_LOSS_REENTRY_BLOCK",
    "FRESH_ENTRY_BLOCK",
    "ANTI_CHASE_BLOCK",
    "ENTRY_TIME_BLOCKED",
    "AUTO_ENTRY_CUTOFF_",
    "SIDEWAYS_BLOCK",
    "MANDATORY_TREND_STRUCTURE_BLOCK",
    "DIRECTION_MISMATCH_BLOCK",
    "EXPIRY_TOO_FAR",
    "OPTION_EXPIRY_",
    "OPTION_CONTRACT_NOT_RESOLVED",
)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _json_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _entry_payload(row: Any) -> dict:
    return _json_dict(_value(row, "entry_explanation_json", ""))


def _explicit_marker(row: Any, payload: dict) -> Optional[str]:
    searchable = "\n".join(
        [
            str(_value(row, "reason", "") or ""),
            json.dumps(payload, ensure_ascii=False, default=str),
        ]
    ).upper()
    for marker in STRONG_BLOCK_MARKERS:
        if marker in searchable:
            return marker
    return None


def _proof(row: Any) -> Optional[dict]:
    if not _paper_mode(row):
        return None

    side = str(_value(row, "side", "") or "").upper().strip()
    qty = _i(_value(row, "qty", 0), 0)
    entry = _f(_value(row, "entry_price", 0), 0)
    payload = _entry_payload(row)

    if side not in {"CE", "PE"}:
        return {
            "reason": "INVALID_OPTION_SIDE",
            "side": side,
        }
    if qty <= 0:
        return {
            "reason": "INVALID_ENTRY_QUANTITY",
            "qty": qty,
        }
    if entry <= 0:
        return {
            "reason": "INVALID_ENTRY_PRICE",
            "entry_price": entry,
        }

    if payload:
        score = _i(payload.get("score"), 0)
        min_score = _i(payload.get("min_score"), 0)
        if score > 0 and min_score > 0 and score < min_score:
            return {
                "reason": "SAVED_SCORE_BELOW_SAVED_THRESHOLD",
                "score": score,
                "min_score": min_score,
                "entry_explanation_version": payload.get("version"),
            }

        quality = payload.get("quality")
        if isinstance(quality, dict) and quality.get("allowed") is False:
            return {
                "reason": "SAVED_OPTION_QUALITY_FAILED",
                "quality_reason": str(quality.get("reason") or ""),
                "entry_explanation_version": payload.get("version"),
            }

        failed_checks = []
        for item in payload.get("checks") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "").upper() == "FAIL":
                failed_checks.append(
                    {
                        "label": str(item.get("label") or ""),
                        "value": str(item.get("value") or ""),
                    }
                )
        if failed_checks:
            return {
                "reason": "SAVED_ENTRY_CHECK_FAILED",
                "failed_checks": failed_checks,
                "entry_explanation_version": payload.get("version"),
            }

    marker = _explicit_marker(row, payload)
    if marker:
        return {
            "reason": "EXPLICIT_ENTRY_BLOCK_MARKER_SAVED",
            "marker": marker,
        }

    entry_day = _entry_day_ist(row)
    expiry_day = _expiry(row)
    if entry_day is not None and expiry_day is not None and expiry_day < entry_day:
        return {
            "reason": "CONTRACT_EXPIRED_BEFORE_ENTRY",
            "entry_day_ist": entry_day.isoformat(),
            "expiry": expiry_day.isoformat(),
            "expiry_dte": (expiry_day - entry_day).days,
        }

    return None


def _ensure_archive(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invalid_blocked_trades_archive (
            trade_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            underlying TEXT,
            original_status TEXT,
            original_pnl REAL,
            audit_reason TEXT NOT NULL,
            audit_json TEXT NOT NULL,
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
        ("DELETE FROM auto_user_cooldowns WHERE user_id=?", (user_id,)),
        ("DELETE FROM auto_reentry_blocks WHERE user_id=?", (user_id,)),
    )
    for sql, params in statements:
        try:
            conn.execute(sql, params)
        except Exception:
            pass


def cleanup_proven_invalid_paper_trades() -> dict:
    """Remove only PAPER rows that carry explicit, row-level invalidity proof."""
    conn = get_db()
    removed = 0
    removed_pnl = 0.0
    affected_users: set[int] = set()
    by_reason: dict[str, int] = {}

    try:
        _ensure_archive(conn)
        rows = conn.execute(
            "SELECT * FROM paper_trades ORDER BY id ASC"
        ).fetchall()

        for row in rows:
            audit = _proof(row)
            if not audit:
                continue

            trade_id = _i(_value(row, "id", 0), 0)
            user_id = _i(_value(row, "user_id", 0), 0)
            if trade_id <= 0:
                continue

            payload = dict(row)
            pnl = _f(_value(row, "net_pnl", _value(row, "pnl", 0)), 0)
            reason = str(audit.get("reason") or "PROVEN_INVALID_PAPER_ENTRY")

            conn.execute(
                """
                INSERT OR IGNORE INTO invalid_blocked_trades_archive (
                    trade_id, user_id, underlying, original_status,
                    original_pnl, audit_reason, audit_json,
                    cleanup_version, archived_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    user_id,
                    _underlying(row),
                    str(_value(row, "status", "") or ""),
                    pnl,
                    reason,
                    json.dumps(audit, ensure_ascii=False, default=str),
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
            by_reason[reason] = by_reason.get(reason, 0) + 1

        conn.commit()
        return {
            "removed": removed,
            "affected_users": len(affected_users),
            "removed_recorded_pnl": round(removed_pnl, 2),
            "by_reason": by_reason,
            "live_trades_removed": 0,
            "cleanup_version": VERSION,
        }
    finally:
        conn.close()
