"""Keep open-position option quotes fresh across Railway restarts.

The mobile Active Trade card reads the monitor-persisted ``last_ltp`` value. A
Railway deploy can preserve the OPEN database row while stopping the in-memory
broker monitor, so a newly generated API response may otherwise repeat an old
quote. This module:

* timestamps every successful runtime quote persisted for an open position;
* restarts persisted running users that still have OPEN positions at startup;
* self-recovers the runtime when /bot/trade-live is requested after a restart;
* disables HTTP/proxy caching for live-trade responses.

No signal, entry, exit, SL, quantity, broker-fill or strategy rule is changed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware

from auth.routes import get_current_user
from bot import auto_portfolio_runtime as runtime
from database import get_db


VERSION = "LIVE_QUOTE_RUNTIME_RECOVERY_V2"
LIVE_PATHS = {"/bot/trade-live"}
_QUOTE_COLUMNS = {"quote_updated_at", "quote_source"}
_quote_columns_ready = False
_quote_columns_lock = threading.Lock()
STALE_RUNTIME_SECONDS = 20
STALE_RESTART_COOLDOWN_SECONDS = 45
_restart_locks: dict[int, threading.Lock] = {}
_restart_locks_guard = threading.Lock()
_last_restart_attempt: dict[int, float] = {}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _column_names(conn) -> set[str]:
    try:
        rows = conn.execute("PRAGMA table_info(paper_trades)").fetchall()
    except Exception:
        return set()

    names: set[str] = set()
    for row in rows:
        try:
            names.add(str(row["name"]))
        except Exception:
            try:
                names.add(str(row[1]))
            except Exception:
                pass
    return names


def _ensure_quote_columns(conn) -> None:
    global _quote_columns_ready
    if _quote_columns_ready:
        return

    with _quote_columns_lock:
        if _quote_columns_ready:
            return

        existing = _column_names(conn)
        for name, kind in [
            ("quote_updated_at", "TEXT"),
            ("quote_source", "TEXT"),
        ]:
            if name in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
            except Exception:
                pass
        conn.commit()
        _quote_columns_ready = _QUOTE_COLUMNS.issubset(_column_names(conn))


def apply_live_quote_timestamp_patch() -> None:
    """Persist the actual successful broker-quote time on every monitor tick."""
    if getattr(runtime, "_okai_live_quote_timestamp_v1", False):
        return

    previous_ensure_schema = runtime._ensure_schema
    previous_update_open = runtime._update_open

    def ensure_schema_with_quote_time(conn):
        previous_ensure_schema(conn)
        _ensure_quote_columns(conn)

    def update_open_with_quote_time(conn, trade, ltp, evaluation):
        previous_update_open(conn, trade, ltp, evaluation)
        _ensure_quote_columns(conn)
        broker = str(runtime._v(trade, "broker_name", "broker") or "broker").upper()
        try:
            conn.execute(
                """
                UPDATE paper_trades
                SET quote_updated_at=?, quote_source=?
                WHERE id=? AND status='OPEN'
                """,
                (_utc_now(), f"{broker}_RUNTIME_LTP", trade["id"]),
            )
            conn.commit()
        except Exception:
            pass

    runtime._ensure_schema = ensure_schema_with_quote_time
    runtime._update_open = update_open_with_quote_time
    runtime._okai_live_quote_timestamp_v1 = True
    runtime._okai_live_quote_timestamp_version = VERSION


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            parsed = datetime.fromisoformat(text[:-1] + "+00:00")
        else:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _open_quote_health(conn, user_id: int) -> dict[str, Any]:
    """Return true stale state only when every open quote has stopped."""
    _ensure_quote_columns(conn)
    try:
        rows = conn.execute(
            """
            SELECT quote_updated_at
            FROM paper_trades
            WHERE user_id=? AND status='OPEN'
            """,
            (int(user_id),),
        ).fetchall()
    except Exception:
        rows = []

    now = datetime.now(timezone.utc)
    ages: list[float | None] = []
    for row in rows:
        try:
            value = row["quote_updated_at"]
        except Exception:
            value = row[0] if row else None
        parsed = _parse_utc(value)
        ages.append(max(0.0, (now - parsed).total_seconds()) if parsed else None)

    stale_count = sum(
        age is None or age > STALE_RUNTIME_SECONDS
        for age in ages
    )
    return {
        "open_count": len(ages),
        "stale_count": stale_count,
        "all_stale": bool(ages) and stale_count == len(ages),
        "oldest_quote_age_seconds": (
            round(max(age for age in ages if age is not None), 1)
            if any(age is not None for age in ages)
            else None
        ),
    }


def _restart_lock(user_id: int) -> threading.Lock:
    uid = int(user_id)
    with _restart_locks_guard:
        lock = _restart_locks.get(uid)
        if lock is None:
            lock = threading.Lock()
            _restart_locks[uid] = lock
        return lock


def _restart_stale_runtime(user_id: int) -> dict[str, Any]:
    """Replace a wedged broker session, with one restart per cooldown window."""
    uid = int(user_id)
    lock = _restart_lock(uid)
    if not lock.acquire(blocking=False):
        return {"attempted": False, "started": False, "restart_in_progress": True}

    try:
        now = time.monotonic()
        last_attempt = _last_restart_attempt.get(uid, 0.0)
        if now - last_attempt < STALE_RESTART_COOLDOWN_SECONDS:
            return {
                "attempted": False,
                "started": False,
                "restart_cooldown": True,
            }

        _last_restart_attempt[uid] = now
        try:
            from bot.angel_fetcher import stop_user_bot

            stop_user_bot(uid)
        except Exception:
            pass

        recovery = _start_runtime(uid)
        return {
            "attempted": True,
            "started": bool(recovery.get("started")),
            "reason": recovery.get("reason"),
            "stale_runtime_restarted": bool(recovery.get("started")),
        }
    finally:
        lock.release()


def _persisted_running(conn, user_id: int) -> bool:
    for table in ("user_bot_state", "bot_status"):
        try:
            row = conn.execute(
                f"SELECT is_running FROM {table} WHERE user_id=? LIMIT 1",
                (int(user_id),),
            ).fetchone()
            if row is not None:
                try:
                    return bool(row["is_running"])
                except Exception:
                    return bool(row[0])
        except Exception:
            continue
    return False


def _open_user_ids(conn) -> list[int]:
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT user_id
            FROM paper_trades
            WHERE status='OPEN'
            ORDER BY user_id ASC
            """
        ).fetchall()
    except Exception:
        return []

    output: list[int] = []
    for row in rows:
        try:
            user_id = int(row["user_id"])
        except Exception:
            try:
                user_id = int(row[0])
            except Exception:
                continue
        if _persisted_running(conn, user_id):
            output.append(user_id)
    return output


def _runtime_state(user_id: int) -> dict[str, Any]:
    try:
        from bot.angel_fetcher import get_user_bot_state

        state = get_user_bot_state(int(user_id))
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _start_runtime(user_id: int) -> dict[str, Any]:
    try:
        from bot.routes import _start_saved_runtime_engine

        result = _start_saved_runtime_engine(int(user_id))
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        return {
            "started": False,
            "reason": f"{type(exc).__name__}:{str(exc)[:160]}",
        }


def recover_user_runtime_if_needed(user_id: int) -> dict[str, Any]:
    """Recover a missing runtime or replace a running-but-wedged quote feed."""
    state = _runtime_state(user_id)
    if state.get("running"):
        conn = get_db()
        try:
            should_run = _persisted_running(conn, int(user_id))
            health = _open_quote_health(conn, int(user_id))
        except Exception:
            should_run = False
            health = {"open_count": 0, "stale_count": 0, "all_stale": False}
        finally:
            conn.close()

        if should_run and health.get("all_stale"):
            restarted = _restart_stale_runtime(user_id)
            return {
                **restarted,
                **health,
                "already_running": False,
                "version": VERSION,
            }

        return {
            "attempted": False,
            "started": False,
            "already_running": True,
            **health,
            "version": VERSION,
        }

    conn = get_db()
    try:
        should_run = _persisted_running(conn, int(user_id))
        has_open = bool(
            conn.execute(
                """
                SELECT 1 FROM paper_trades
                WHERE user_id=? AND status='OPEN'
                LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
        )
    except Exception:
        should_run = False
        has_open = False
    finally:
        conn.close()

    if not should_run or not has_open:
        return {
            "attempted": False,
            "started": False,
            "already_running": False,
            "persisted_running": should_run,
            "has_open_trade": has_open,
            "version": VERSION,
        }

    recovery = _start_runtime(user_id)
    return {
        "attempted": True,
        "started": bool(recovery.get("started")),
        "already_running": False,
        "reason": recovery.get("reason"),
        "version": VERSION,
    }


def recover_persisted_open_trade_engines() -> dict[str, Any]:
    """Start monitor threads for persisted ON users immediately after deploy."""
    conn = get_db()
    try:
        _ensure_quote_columns(conn)
        user_ids = _open_user_ids(conn)
    finally:
        conn.close()

    started = 0
    failed: list[dict[str, Any]] = []
    for user_id in user_ids:
        result = recover_user_runtime_if_needed(user_id)
        if result.get("started") or result.get("already_running"):
            started += 1
        elif result.get("attempted"):
            failed.append(
                {
                    "user_id": user_id,
                    "reason": result.get("reason"),
                }
            )

    return {
        "eligible_users": len(user_ids),
        "running_or_started": started,
        "failed": failed,
        "version": VERSION,
    }


class TradeLiveRuntimeRecoveryMiddleware(BaseHTTPMiddleware):
    """Recover stopped open-trade monitors and forbid cached live responses."""

    async def dispatch(self, request, call_next):
        recovered = False
        recovery_attempted = False

        if request.url.path in LIVE_PATHS:
            authorization = request.headers.get("authorization")
            try:
                user = get_current_user(authorization)
                result = await asyncio.to_thread(
                    recover_user_runtime_if_needed,
                    int(user["id"]),
                )
                recovered = bool(result.get("started"))
                recovery_attempted = bool(result.get("attempted"))
            except Exception:
                pass

        response = await call_next(request)

        if request.url.path in LIVE_PATHS:
            response.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, max-age=0"
            )
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            response.headers["X-OKAI-Live-Quote-Version"] = VERSION
            response.headers["X-OKAI-Runtime-Recovery-Attempted"] = (
                "1" if recovery_attempted else "0"
            )
            response.headers["X-OKAI-Runtime-Recovered"] = (
                "1" if recovered else "0"
            )

        return response
