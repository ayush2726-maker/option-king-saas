"""Shared owner-broker market-data feed for PAPER users without credentials.

New trial/testing users may run their own isolated PAPER bot and trade history by
reading market data through the owner's selected broker.  Credentials are never
copied to another user, returned by an API, or used for LIVE orders.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any

from fastapi import Header

from auth.utils import decrypt_credential
from database import get_db


_ENABLED_VALUES = {"1", "true", "yes", "on"}
_SHARED_SESSION_TTL_SECONDS = 6 * 60 * 60
_CANDLE_CACHE_SECONDS = 10.0
_LTP_CACHE_SECONDS = 0.75

_shared_session_lock = threading.RLock()
_shared_angel_sessions: dict[str, dict[str, Any]] = {}


def _enabled() -> bool:
    return str(
        os.getenv("OKAI_SHARED_TRIAL_PAPER_FEED", "true")
    ).strip().lower() in _ENABLED_VALUES


def _row_value(row, key, default=None):
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        return default


def _selected_personal_broker(conn, user_id: int):
    return conn.execute(
        """SELECT * FROM broker_credentials
           WHERE user_id=? AND is_active=1
           ORDER BY last_connected DESC, id DESC
           LIMIT 1""",
        (int(user_id),),
    ).fetchone()


def _eligible_shared_user(conn, user_id: int) -> bool:
    if not _enabled():
        return False

    user = conn.execute(
        """SELECT id, is_admin, is_active, subscription_status
           FROM users WHERE id=? LIMIT 1""",
        (int(user_id),),
    ).fetchone()
    if not user:
        return False
    if bool(_row_value(user, "is_admin", 0)):
        return False
    if not bool(_row_value(user, "is_active", 1)):
        return False

    # During the current testing release users may be normalised to `active`.
    # Keep expired/suspended accounts out, while allowing trial and paid users
    # without a personal broker to use PAPER market data.
    status = str(
        _row_value(user, "subscription_status", "trial") or "trial"
    ).lower()
    return status in {"trial", "active"}


def _selected_owner_broker(conn, requester_user_id: int):
    admin_email = str(os.getenv("ADMIN_EMAIL", "") or "").strip().lower()

    if admin_email:
        row = conn.execute(
            """SELECT bc.*, u.id AS source_user_id
               FROM users u
               JOIN broker_credentials bc
                 ON bc.user_id=u.id AND bc.is_active=1
               WHERE u.is_admin=1
                 AND u.is_active=1
                 AND LOWER(u.email)=?
                 AND u.id<>?
               ORDER BY bc.last_connected DESC, bc.id DESC
               LIMIT 1""",
            (admin_email, int(requester_user_id)),
        ).fetchone()
        if row:
            return row

    return conn.execute(
        """SELECT bc.*, u.id AS source_user_id
           FROM users u
           JOIN broker_credentials bc
             ON bc.user_id=u.id AND bc.is_active=1
           WHERE u.is_admin=1
             AND u.is_active=1
             AND u.id<>?
           ORDER BY bc.last_connected DESC, bc.id DESC
           LIMIT 1""",
        (int(requester_user_id),),
    ).fetchone()


def resolve_paper_broker_source(conn, user_id: int):
    """Return (row, source) without ever copying broker credentials."""
    personal = _selected_personal_broker(conn, user_id)
    if personal:
        return personal, "personal"

    if not _eligible_shared_user(conn, user_id):
        return None, None

    owner = _selected_owner_broker(conn, user_id)
    if owner:
        return owner, "shared_owner_paper"
    return None, None


def _credentials_from_row(row, shared: bool = False) -> dict[str, Any]:
    credentials = {
        "api_key": decrypt_credential(row["api_key"]),
        "client_id": row["client_id"],
        "password": decrypt_credential(row["api_secret"]),
        "totp_secret": (
            decrypt_credential(row["totp_secret"])
            if _row_value(row, "totp_secret")
            else None
        ),
    }
    if shared:
        credentials["_shared_paper_feed"] = True
        credentials["_shared_source_user_id"] = int(
            _row_value(row, "source_user_id", _row_value(row, "user_id", 0)) or 0
        )
    return credentials


def _fingerprint(credentials: dict[str, Any]) -> str:
    raw = "|".join(
        str(credentials.get(key) or "")
        for key in ("client_id", "api_key", "password", "totp_secret")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _auth_failed(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("status") is not False:
        return False
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("message", "errorcode", "errorCode", "data")
    ).lower()
    return any(
        word in text
        for word in ("token", "session", "jwt", "unauthor", "login")
    )


class _SharedAngelProxy:
    """Serialised SmartAPI session with tiny cross-user quote/candle caches."""

    def __init__(self, target, cache_key: str, invalidate):
        self._target = target
        self._cache_key = cache_key
        self._invalidate = invalidate
        self._call_lock = threading.RLock()
        self._candle_cache: dict[str, tuple[float, Any]] = {}
        self._ltp_cache: dict[str, tuple[float, Any]] = {}

    def _cached_call(self, cache, key: str, ttl: float, function, *args, **kwargs):
        now = time.monotonic()
        with self._call_lock:
            cached = cache.get(key)
            if cached and now - cached[0] <= ttl:
                return cached[1]
            result = function(*args, **kwargs)
            if _auth_failed(result):
                self._invalidate(self._cache_key)
            cache[key] = (now, result)
            return result

    def getCandleData(self, params):
        key = json.dumps(params or {}, sort_keys=True, default=str)
        return self._cached_call(
            self._candle_cache,
            key,
            _CANDLE_CACHE_SECONDS,
            self._target.getCandleData,
            params,
        )

    def ltpData(self, exchange, symbol, token):
        key = f"{exchange}|{symbol}|{token}"
        return self._cached_call(
            self._ltp_cache,
            key,
            _LTP_CACHE_SECONDS,
            self._target.ltpData,
            exchange,
            symbol,
            token,
        )

    def __getattr__(self, name):
        attribute = getattr(self._target, name)
        if not callable(attribute):
            return attribute

        def guarded(*args, **kwargs):
            with self._call_lock:
                result = attribute(*args, **kwargs)
                if _auth_failed(result):
                    self._invalidate(self._cache_key)
                return result

        return guarded


def _invalidate_shared_session(cache_key: str) -> None:
    with _shared_session_lock:
        _shared_angel_sessions.pop(cache_key, None)


def _patch_shared_angel_login(angel_fetcher) -> None:
    if getattr(angel_fetcher, "_okai_shared_trial_angel_login_v1", False):
        return

    original_login = angel_fetcher.angel_login

    def shared_aware_login(credentials: dict[str, Any]):
        if not credentials.get("_shared_paper_feed"):
            return original_login(credentials)

        cache_key = _fingerprint(credentials)
        now = time.monotonic()
        with _shared_session_lock:
            cached = _shared_angel_sessions.get(cache_key)
            if cached and now - cached["created_at"] < _SHARED_SESSION_TTL_SECONDS:
                return cached["proxy"]

        target = original_login(credentials)
        proxy = _SharedAngelProxy(
            target,
            cache_key=cache_key,
            invalidate=_invalidate_shared_session,
        )
        with _shared_session_lock:
            _shared_angel_sessions[cache_key] = {
                "created_at": now,
                "proxy": proxy,
            }
        return proxy

    angel_fetcher.angel_login = shared_aware_login
    angel_fetcher._okai_shared_trial_angel_login_v1 = True


def _mark_shared_state(angel_fetcher, user_id: int, broker_name: str) -> dict:
    state = angel_fetcher.get_user_bot_state(int(user_id))
    if isinstance(state, dict):
        state.update(
            {
                "shared_paper_feed": True,
                "market_data_source": "SHARED_OWNER_PAPER_FEED",
                "data_broker": broker_name,
                "live_orders_enabled": False,
            }
        )
    return state


def _start_from_row(angel_fetcher, user_id: int, row, source: str):
    broker_name = str(_row_value(row, "broker_name", "angelone") or "angelone").lower()
    credentials = _credentials_from_row(row, shared=source == "shared_owner_paper")

    if broker_name == "angelone":
        result = angel_fetcher.start_user_bot(int(user_id), credentials)
    else:
        result = angel_fetcher.start_user_bot_multi(
            int(user_id), broker_name, credentials
        )

    if source == "shared_owner_paper":
        _mark_shared_state(angel_fetcher, user_id, broker_name)
    return result, broker_name


def _set_persisted_stopped(routes, user_id: int) -> None:
    conn = get_db()
    try:
        routes.ensure_tables(conn)
        routes.save_bot_status(conn, int(user_id), 0, "SHARED_FEED_UNAVAILABLE")
    finally:
        conn.close()


def apply_shared_trial_paper_feed_patch() -> None:
    """Patch PAPER start/recovery before bot_router is included in FastAPI."""
    from bot import angel_fetcher
    from bot import routes

    if getattr(routes, "_okai_shared_trial_paper_feed_v1", False):
        return

    _patch_shared_angel_login(angel_fetcher)

    original_start = routes.bot_start
    original_recovery = routes._start_saved_runtime_engine

    def shared_paper_bot_start(authorization: str = Header(None)):
        user = routes.get_current_user(authorization)
        user_id = int(user["id"])

        conn = get_db()
        try:
            routes.ensure_tables(conn)
            settings = routes.get_strategy_settings(conn, user_id)
            trading_mode = str(settings.get("trading_mode", "paper") or "paper").lower()
            personal = _selected_personal_broker(conn, user_id)
        finally:
            conn.close()

        # LIVE is intentionally unchanged and always requires the user's own broker.
        if trading_mode == "live" or personal:
            return original_start(authorization)

        # Let the existing route persist PAPER running state and notifications first.
        response = original_start(authorization)

        conn = get_db()
        try:
            row, source = resolve_paper_broker_source(conn, user_id)
        finally:
            conn.close()

        if not row or source != "shared_owner_paper":
            _set_persisted_stopped(routes, user_id)
            return {
                "success": False,
                "message": (
                    "Shared PAPER market-data feed is unavailable. "
                    "Admin broker ko reconnect karna hoga."
                ),
                "mode": "paper",
                "shared_paper_feed": False,
                "real_orders": False,
            }

        try:
            result, broker_name = _start_from_row(
                angel_fetcher, user_id, row, source
            )
            state = _mark_shared_state(angel_fetcher, user_id, broker_name)
            started = bool(state.get("running")) or bool(
                isinstance(result, dict)
                and (
                    result.get("success")
                    or result.get("message") == "Bot already running"
                )
            )
            if not started:
                raise RuntimeError(
                    str((result or {}).get("message") or "Shared engine start failed")
                )
        except Exception as exc:
            _set_persisted_stopped(routes, user_id)
            return {
                "success": False,
                "message": "Shared PAPER feed start failed: " + str(exc)[:160],
                "mode": "paper",
                "shared_paper_feed": False,
                "real_orders": False,
            }

        output = dict(response or {})
        output.update(
            {
                "success": True,
                "message": (
                    f"PAPER bot started on shared {broker_name.upper()} market data. "
                    "Aapki trades aur P&L alag rahenge; owner credentials hidden hain; "
                    "real orders OFF."
                ),
                "mode": "paper",
                "shared_paper_feed": True,
                "market_data_source": "shared_owner_paper_feed",
                "data_broker": broker_name,
                "real_orders": False,
                "personal_broker_required": False,
            }
        )
        return output

    def shared_recovery(user_id: int):
        recovered = original_recovery(int(user_id))
        if recovered.get("started") or recovered.get("reason") != "BROKER_NOT_CONNECTED":
            return recovered

        conn = get_db()
        try:
            routes.ensure_tables(conn)
            settings = routes.get_strategy_settings(conn, int(user_id))
            if str(settings.get("trading_mode", "paper") or "paper").lower() != "paper":
                return recovered
            row, source = resolve_paper_broker_source(conn, int(user_id))
        finally:
            conn.close()

        if not row or source != "shared_owner_paper":
            return recovered

        try:
            result, broker_name = _start_from_row(
                angel_fetcher, int(user_id), row, source
            )
            state = _mark_shared_state(angel_fetcher, int(user_id), broker_name)
            started = bool(state.get("running")) or bool(
                isinstance(result, dict) and result.get("success")
            )
            return {
                "state": state,
                "started": started,
                "reason": None if started else str((result or {}).get("message") or "ENGINE_START_FAILED"),
                "shared_paper_feed": started,
            }
        except Exception as exc:
            return {
                "state": angel_fetcher.get_user_bot_state(int(user_id)),
                "started": False,
                "reason": "SHARED_PAPER_FEED_FAILED: " + str(exc)[:120],
            }

    routes.bot_start = shared_paper_bot_start
    routes._start_saved_runtime_engine = shared_recovery

    # The decorator already created an APIRoute. Replace its callable before
    # main.py includes bot_router in the application.
    for route in routes.router.routes:
        if getattr(route, "path", None) == "/bot/start" and "POST" in getattr(route, "methods", set()):
            route.endpoint = shared_paper_bot_start
            if getattr(route, "dependant", None) is not None:
                route.dependant.call = shared_paper_bot_start

    routes._okai_shared_trial_paper_feed_v1 = True
