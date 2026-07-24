"""Final AUTO EOD safety, invalid-trade cleanup and testing access.

This module is intentionally applied after every strategy/runtime wrapper so the
last active AUTO Portfolio entry path cannot reopen a position after the normal
entry cutoff.  PAPER observation remains unlimited during the valid session;
LIVE limits and every existing quality/risk gate remain unchanged.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from bot import auto_portfolio_runtime as runtime
from database import get_db


IST = ZoneInfo("Asia/Kolkata")
AUTO_ENTRY_START_MINUTE = 9 * 60 + 15
AUTO_ENTRY_CUTOFF_MINUTE = 14 * 60 + 45
HARD_EOD_MINUTE = 15 * 60 + 25

_ACCESS_REFRESH_SECONDS = 30
_access_lock = threading.Lock()
_last_access_refresh = 0.0

_ACCESS_RESPONSE_PATHS = {
    "/auth/register",
    "/auth/login",
    "/auth/me",
    "/subscription/status",
}
_NO_CACHE_PREFIXES = (
    "/bot/signal",
    "/bot/trade-live",
    "/bot/trade-history",
    "/history/paper",
    "/reports/daily",
)


def _now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST)


def _minute_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _entry_window_open(value: datetime | None = None) -> bool:
    current = value or _now_ist()
    minute = _minute_of_day(current)
    return (
        current.weekday() < 5
        and AUTO_ENTRY_START_MINUTE <= minute < AUTO_ENTRY_CUTOFF_MINUTE
    )


def _entry_block_reason(value: datetime) -> str:
    if value.weekday() >= 5:
        return "AUTO_ENTRY_BLOCKED_MARKET_CLOSED"
    if _minute_of_day(value) < AUTO_ENTRY_START_MINUTE:
        return "AUTO_ENTRY_BLOCKED_BEFORE_0915_IST"
    return "AUTO_ENTRY_CUTOFF_1445_IST"


def _mark_entry_time_block(state: dict | None, value: datetime) -> None:
    if not isinstance(state, dict):
        return
    state.update(
        {
            "entry_time_blocked": True,
            "entry_time_block_reason": _entry_block_reason(value),
            "entry_window_ist": "09:15-14:45",
            "hard_eod_exit_ist": "15:25",
            "selected_for_entry": None,
        }
    )


def _clear_entry_time_block(state: dict | None) -> None:
    if not isinstance(state, dict):
        return
    state["entry_time_blocked"] = False
    state.pop("entry_time_block_reason", None)
    state["entry_window_ist"] = "09:15-14:45"
    state["hard_eod_exit_ist"] = "15:25"


def apply_eod_entry_guard_patch() -> None:
    """Install a final, defence-in-depth clock gate on every AUTO entry."""
    if getattr(runtime, "_okai_final_eod_entry_guard_v1", False):
        return

    original_can_enter = runtime._can_enter
    original_open_common = runtime._open_common

    def can_enter_with_clock_guard(conn, user_id, settings, rows, state):
        current = _now_ist()
        if not _entry_window_open(current):
            _mark_entry_time_block(state, current)
            return False
        _clear_entry_time_block(state)
        return original_can_enter(conn, user_id, settings, rows, state)

    def open_common_with_clock_guard(
        conn,
        user_id,
        broker_name,
        selected,
        settings,
        resolved,
        quote_price,
        quality,
        lot_size,
        live_order,
        live_cash,
        state,
    ):
        current = _now_ist()
        if not _entry_window_open(current):
            _mark_entry_time_block(state, current)
            return False
        _clear_entry_time_block(state)
        return original_open_common(
            conn,
            user_id,
            broker_name,
            selected,
            settings,
            resolved,
            quote_price,
            quality,
            lot_size,
            live_order,
            live_cash,
            state,
        )

    runtime._can_enter = can_enter_with_clock_guard
    runtime._open_common = open_common_with_clock_guard
    runtime._okai_final_eod_entry_guard_v1 = True


def _parse_utc_timestamp(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
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
        # Backend/SQLite naive timestamps in this project are UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def cleanup_invalid_eod_paper_entries() -> int:
    """Archive and remove PAPER entries that were opened at/after 15:25 IST.

    Only the invalid entry row is removed.  A valid position opened earlier and
    closed by the 15:25 EOD exit remains in history.
    """
    conn = get_db()
    removed = 0
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invalid_eod_trades_archive (
                trade_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                created_at TEXT,
                archived_at TEXT NOT NULL,
                cleanup_reason TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        rows = conn.execute("SELECT * FROM paper_trades ORDER BY id ASC").fetchall()
        invalid_ids: list[int] = []
        for row in rows:
            trade = dict(row)
            mode = str(trade.get("trading_mode") or "paper").lower()
            if mode != "paper":
                continue
            created_utc = _parse_utc_timestamp(trade.get("created_at"))
            if created_utc is None:
                continue
            created_ist = created_utc.astimezone(IST)
            if _minute_of_day(created_ist) < HARD_EOD_MINUTE:
                continue

            trade_id = int(trade.get("id") or 0)
            if trade_id <= 0:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO invalid_eod_trades_archive (
                    trade_id, user_id, created_at, archived_at,
                    cleanup_reason, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    int(trade.get("user_id") or 0),
                    str(trade.get("created_at") or ""),
                    datetime.now(timezone.utc).isoformat(),
                    "INVALID_PAPER_ENTRY_AT_OR_AFTER_1525_IST",
                    json.dumps(trade, ensure_ascii=False, default=str),
                ),
            )
            invalid_ids.append(trade_id)

        for trade_id in invalid_ids:
            conn.execute("DELETE FROM paper_trades WHERE id=?", (trade_id,))
            try:
                conn.execute(
                    "DELETE FROM auto_reentry_blocks WHERE source_trade_id=?",
                    (trade_id,),
                )
            except Exception:
                pass
            removed += 1
        conn.commit()
        return removed
    finally:
        conn.close()


def activate_testing_access(force: bool = False) -> int:
    """Keep every active account fully enabled while public testing is running."""
    global _last_access_refresh
    now = time.monotonic()
    if not force and now - _last_access_refresh < _ACCESS_REFRESH_SECONDS:
        return 0

    with _access_lock:
        now = time.monotonic()
        if not force and now - _last_access_refresh < _ACCESS_REFRESH_SECONDS:
            return 0
        conn = get_db()
        try:
            cursor = conn.execute(
                """
                UPDATE users
                SET subscription_status='active', trial_ends_at=NULL
                WHERE COALESCE(is_active, 1)=1
                  AND (
                    COALESCE(subscription_status, '')<>'active'
                    OR trial_ends_at IS NOT NULL
                  )
                """
            )
            conn.commit()
            changed = int(cursor.rowcount or 0)
        finally:
            conn.close()
        _last_access_refresh = now
        return changed


def initialize_testing_access_and_cleanup() -> dict:
    return {
        "testing_access_users_updated": activate_testing_access(force=True),
        "invalid_eod_paper_trades_removed": cleanup_invalid_eod_paper_entries(),
    }


def _normalize_access_payload(value, path: str):
    if isinstance(value, list):
        return [_normalize_access_payload(item, path) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {
        key: _normalize_access_payload(item, path)
        for key, item in value.items()
    }
    if "subscription_status" in normalized:
        normalized["subscription_status"] = "active"
    if "trial_ends_at" in normalized:
        normalized["trial_ends_at"] = None
    if "warning" in normalized:
        normalized["warning"] = None

    if path == "/subscription/status":
        normalized.update(
            {
                "success": True,
                "subscription_status": "active",
                "days_remaining": None,
                "unlimited": True,
                "testing_access": True,
                "active_subscription": {
                    "plan": "testing_full_access",
                    "status": "active",
                    "valid_from": None,
                    "valid_till": None,
                },
            }
        )
    elif path == "/auth/register":
        normalized["message"] = "Welcome! Full testing access is active."
        normalized["testing_access"] = True
    elif path in {"/auth/login", "/auth/me"}:
        normalized["testing_access"] = True
    return normalized


class TestingFullAccessAndFreshDataMiddleware(BaseHTTPMiddleware):
    """Expose full testing access and prevent stale P&L/history responses."""

    async def dispatch(self, request, call_next):
        try:
            activate_testing_access(force=False)
        except Exception:
            # Trading/history routes must remain available even if access refresh
            # encounters a transient database lock.
            pass

        response = await call_next(request)
        path = request.url.path

        if path in _ACCESS_RESPONSE_PATHS:
            content_type = str(response.headers.get("content-type") or "").lower()
            if "application/json" in content_type:
                body = b""
                async for chunk in response.body_iterator:
                    body += chunk
                try:
                    payload = json.loads(body.decode("utf-8"))
                    payload = _normalize_access_payload(payload, path)
                    body = json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                except Exception:
                    pass

                headers = dict(response.headers)
                headers.pop("content-length", None)
                response = Response(
                    content=body,
                    status_code=response.status_code,
                    headers=headers,
                    media_type="application/json",
                    background=response.background,
                )

        if any(path.startswith(prefix) for prefix in _NO_CACHE_PREFIXES):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        response.headers["X-OKAI-Testing-Access"] = "full"
        response.headers["X-OKAI-Release"] = "eod-reentry-full-access-pnl-sync-v1"
        return response
