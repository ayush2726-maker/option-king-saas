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
from datetime import datetime, timezone
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware

from auth.routes import get_current_user
from bot import auto_portfolio_runtime as runtime
from database import get_db


VERSION = "LIVE_QUOTE_RUNTIME_RECOVERY_V1"
LIVE_PATHS = {"/bot/trade-live"}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _ensure_quote_columns(conn) -> None:
    for name, kind in [
        ("quote_updated_at", "TEXT"),
        ("quote_source", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
        except Exception:
            pass
    conn.commit()


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
    """Restart only when the persisted bot is ON and memory runtime is absent."""
    state = _runtime_state(user_id)
    if state.get("running"):
        return {
            "attempted": False,
            "started": False,
            "already_running": True,
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
