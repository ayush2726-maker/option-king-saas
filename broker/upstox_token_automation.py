"""Semi-automatic daily Upstox access-token renewal.

Upstox standard access tokens expire at 03:30 IST on the following day.  The
supported unattended-friendly flow can initiate a request automatically, but
the account holder must still approve it in Upstox.  Upstox then sends the new
token to this service's notifier webhook.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, time as wall_time, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from auth.routes import get_current_user
from auth.utils import decrypt_credential, encrypt_credential
from database import get_db


router = APIRouter(prefix="/broker/upstox", tags=["Upstox Daily Authentication"])
public_router = APIRouter(prefix="/upstox", tags=["Upstox Webhooks"])

IST = ZoneInfo("Asia/Kolkata")
REQUEST_START_IST = wall_time(8, 30)
REQUEST_END_IST = wall_time(15, 0)
REQUEST_RETRY_SECONDS = 15 * 60
UPSTOX_PROFILE_URL = "https://api.upstox.com/v2/user/profile"
UPSTOX_TOKEN_REQUEST_URL = "https://api.upstox.com/v3/login/auth/token/request/{client_id}"
DEFAULT_PUBLIC_BASE_URL = "https://option-king-saas-production.up.railway.app"

_scheduler_started = False
_scheduler_lock = threading.Lock()


class AutomationUpdate(BaseModel):
    enabled: bool = True


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat()


def _public_base_url() -> str:
    return str(
        os.getenv("OKAI_PUBLIC_BASE_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or DEFAULT_PUBLIC_BASE_URL
    ).strip().rstrip("/")


def notifier_url() -> str:
    return f"{_public_base_url()}/upstox/notifier"


def ensure_upstox_token_schema(conn=None) -> None:
    own = conn is None
    db = conn or get_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS upstox_token_automation (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'not_requested',
                last_request_date TEXT,
                last_request_at TEXT,
                approval_expires_at TEXT,
                last_token_at TEXT,
                token_expires_at TEXT,
                upstox_user_id TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        # Existing Upstox connections are enrolled once. Users can turn this off
        # through the authenticated automation endpoint.
        db.execute(
            """
            INSERT OR IGNORE INTO upstox_token_automation (user_id, enabled)
            SELECT DISTINCT user_id, 1
            FROM broker_credentials
            WHERE LOWER(broker_name)='upstox'
            """
        )
        db.commit()
    finally:
        if own:
            db.close()


def enable_for_saved_upstox(user_id: int, enabled: bool = True) -> None:
    conn = get_db()
    try:
        ensure_upstox_token_schema(conn)
        conn.execute(
            """
            INSERT INTO upstox_token_automation (user_id, enabled, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled=excluded.enabled,
                updated_at=excluded.updated_at
            """,
            (int(user_id), 1 if enabled else 0, _iso_utc()),
        )
        conn.commit()
    finally:
        conn.close()


def _json_payload(response) -> dict:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _api_error(payload: dict, fallback: str) -> str:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        return str(
            first.get("message") or first.get("errorCode") or fallback
        )[:240]
    return (
        str(payload.get("message") or fallback)[:240]
        if isinstance(payload, dict)
        else fallback
    )


def _epoch_ms_to_iso(value) -> str | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _get_upstox_credential(conn, user_id: int):
    return conn.execute(
        """
        SELECT * FROM broker_credentials
        WHERE user_id=? AND LOWER(broker_name)='upstox'
        ORDER BY is_active DESC, last_connected DESC, id DESC
        LIMIT 1
        """,
        (int(user_id),),
    ).fetchone()


def initiate_token_request(user_id: int, http_post=None) -> dict:
    """Ask Upstox to notify the user and await their explicit approval."""
    post = http_post or requests.post
    now = _utc_now()
    today_ist = now.astimezone(IST).date().isoformat()

    conn = get_db()
    try:
        ensure_upstox_token_schema(conn)
        credential = _get_upstox_credential(conn, int(user_id))
        if credential is None:
            raise HTTPException(
                status_code=404,
                detail="Upstox credentials saved nahi hain.",
            )
        client_id = str(credential["client_id"] or "").strip()
        client_secret = decrypt_credential(credential["api_key"])
        if not client_id or not client_secret:
            raise HTTPException(
                status_code=400,
                detail="Upstox API Key/Secret incomplete hain.",
            )

        conn.execute(
            """
            INSERT INTO upstox_token_automation
                (user_id, enabled, status, last_request_at, updated_at)
            VALUES (?, 1, 'requesting', ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                status='requesting', last_request_at=excluded.last_request_at,
                last_error=NULL, updated_at=excluded.updated_at
            """,
            (int(user_id), _iso_utc(now), _iso_utc(now)),
        )
        conn.commit()
    finally:
        conn.close()

    url = UPSTOX_TOKEN_REQUEST_URL.format(client_id=quote(client_id, safe=""))
    try:
        response = post(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"client_secret": client_secret},
            timeout=15,
        )
        payload = _json_payload(response)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not getattr(response, "ok", False) or payload.get("status") != "success":
            raise RuntimeError(
                _api_error(
                    payload,
                    f"Upstox HTTP {getattr(response, 'status_code', 0)}",
                )
            )

        approval_expires_at = _epoch_ms_to_iso(data.get("authorization_expiry"))
        returned_notifier = str(data.get("notifier_url") or "").strip()
        conn = get_db()
        try:
            ensure_upstox_token_schema(conn)
            conn.execute(
                """
                UPDATE upstox_token_automation
                SET status='approval_pending', last_request_date=?,
                    last_request_at=?, approval_expires_at=?, last_error=NULL,
                    updated_at=?
                WHERE user_id=?
                """,
                (
                    today_ist,
                    _iso_utc(now),
                    approval_expires_at,
                    _iso_utc(now),
                    int(user_id),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        expected_notifier = notifier_url()
        notifier_matches = (
            not returned_notifier
            or returned_notifier.rstrip("/") == expected_notifier.rstrip("/")
        )
        return {
            "success": True,
            "status": "approval_pending",
            "message": "Upstox approval request bhej di gayi. App/WhatsApp me Approve karein.",
            "approval_expires_at": approval_expires_at,
            "notifier_configured": returned_notifier or expected_notifier,
            "notifier_matches": notifier_matches,
            "warning": (
                None
                if notifier_matches
                else f"Upstox app me Notifier URL {expected_notifier} save karein."
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc)[:240]
        conn = get_db()
        try:
            ensure_upstox_token_schema(conn)
            conn.execute(
                """
                UPDATE upstox_token_automation
                SET status='request_failed', last_error=?, updated_at=?
                WHERE user_id=?
                """,
                (message, _iso_utc(), int(user_id)),
            )
            conn.commit()
        finally:
            conn.close()
        raise HTTPException(
            status_code=502,
            detail=f"Upstox token request failed: {message}",
        ) from exc


def _validate_received_token(access_token: str, http_get=None) -> dict:
    get = http_get or requests.get
    response = get(
        UPSTOX_PROFILE_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        timeout=12,
    )
    payload = _json_payload(response)
    if not getattr(response, "ok", False) or payload.get("status") != "success":
        raise ValueError("Upstox notifier token validation failed")
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def store_notifier_token(payload: dict, http_get=None) -> dict:
    """Validate a genuine Upstox token and atomically replace the saved token."""
    client_id = str(payload.get("client_id") or "").strip()
    access_token = str(payload.get("access_token") or "").strip()
    claimed_user_id = str(payload.get("user_id") or "").strip()
    if not client_id or not access_token:
        raise HTTPException(status_code=400, detail="Invalid Upstox notifier payload")

    try:
        profile = _validate_received_token(access_token, http_get=http_get)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Upstox token validation failed") from exc

    verified_user_id = str(
        profile.get("user_id") or profile.get("userId") or ""
    ).strip()
    if claimed_user_id and verified_user_id and claimed_user_id != verified_user_id:
        raise HTTPException(status_code=401, detail="Upstox user verification failed")

    now = _utc_now()
    expires_at = _epoch_ms_to_iso(payload.get("expires_at"))
    conn = get_db()
    user_id = None
    selected = False
    should_restart = False
    try:
        ensure_upstox_token_schema(conn)
        rows = conn.execute(
            """
            SELECT * FROM broker_credentials
            WHERE LOWER(broker_name)='upstox' AND client_id=?
            ORDER BY is_active DESC, last_connected DESC, id DESC
            """,
            (client_id,),
        ).fetchall()
        if len(rows) != 1:
            # A client ID must identify one saved account; never guess between
            # duplicate rows because a token controls real trading authority.
            raise HTTPException(
                status_code=409 if rows else 404,
                detail="Upstox API Key mapping unique nahi hai.",
            )
        credential = rows[0]
        user_id = int(credential["user_id"])
        selected = bool(credential["is_active"])
        for table in ("user_bot_state", "bot_status"):
            try:
                row = conn.execute(
                    f"SELECT is_running FROM {table} WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                should_restart = should_restart or bool(row and row["is_running"])
            except Exception:
                pass

        conn.execute(
            """
            UPDATE broker_credentials
            SET api_secret=?, last_connected=?
            WHERE id=?
            """,
            (
                encrypt_credential(access_token),
                _iso_utc(now),
                int(credential["id"]),
            ),
        )
        conn.execute(
            """
            INSERT INTO upstox_token_automation
                (user_id, enabled, status, last_token_at, token_expires_at,
                 upstox_user_id, last_error, updated_at)
            VALUES (?, 1, 'connected', ?, ?, ?, NULL, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled=1, status='connected', last_token_at=excluded.last_token_at,
                token_expires_at=excluded.token_expires_at,
                upstox_user_id=excluded.upstox_user_id, last_error=NULL,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                _iso_utc(now),
                expires_at,
                verified_user_id or claimed_user_id or None,
                _iso_utc(now),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    restarted = False
    if selected:
        try:
            from broker.routes import _invalidate_test_result, _stop_stale_broker_runtime

            _invalidate_test_result(user_id, "upstox")
            _stop_stale_broker_runtime(user_id)
            if should_restart:
                from bot.routes import _start_saved_runtime_engine

                restart_result = _start_saved_runtime_engine(user_id)
                restarted = bool(
                    restart_result.get("started")
                    or (restart_result.get("state") or {}).get("running")
                )
        except Exception:
            restarted = False

    try:
        from telegram.routes import notify_user

        notify_user(
            user_id,
            "✅ <b>Upstox Connected</b>\n"
            "Daily access token automatically save ho gaya.\n"
            "Termux/copy-paste ki zarurat nahi.",
        )
    except Exception:
        pass

    return {
        "success": True,
        "status": "connected",
        "token_expires_at": expires_at,
        "runtime_restarted": restarted,
    }


def _parse_iso(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def token_status_for_user(user_id: int) -> dict:
    conn = get_db()
    try:
        ensure_upstox_token_schema(conn)
        credential = _get_upstox_credential(conn, int(user_id))
        row = conn.execute(
            "SELECT * FROM upstox_token_automation WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    finally:
        conn.close()

    if credential is None:
        return {
            "configured": False,
            "enabled": False,
            "status": "not_configured",
            "notifier_url": notifier_url(),
        }

    item = dict(row) if row is not None else {}
    expiry = _parse_iso(item.get("token_expires_at"))
    status = str(item.get("status") or "not_requested")
    if expiry is not None and expiry <= _utc_now() and status == "connected":
        status = "token_expired"
    return {
        "configured": True,
        "enabled": bool(item.get("enabled", 1)),
        "selected": bool(credential["is_active"]),
        "status": status,
        "approval_pending": status == "approval_pending",
        "last_request_at": item.get("last_request_at"),
        "approval_expires_at": item.get("approval_expires_at"),
        "last_token_at": item.get("last_token_at"),
        "token_expires_at": item.get("token_expires_at"),
        "last_error": item.get("last_error"),
        "notifier_url": notifier_url(),
        "daily_action": "Upstox notification me Approve dabayein.",
    }


@router.get("/token-status")
def get_token_status(authorization: str = Header(None)):
    user = get_current_user(authorization)
    return {"success": True, **token_status_for_user(int(user["id"]))}


@router.post("/token-request")
def request_token_now(authorization: str = Header(None)):
    user = get_current_user(authorization)
    return initiate_token_request(int(user["id"]))


@router.post("/automation")
def update_automation(body: AutomationUpdate, authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    try:
        if _get_upstox_credential(conn, int(user["id"])) is None:
            raise HTTPException(
                status_code=404,
                detail="Upstox credentials saved nahi hain.",
            )
    finally:
        conn.close()
    enable_for_saved_upstox(int(user["id"]), body.enabled)
    return {
        "success": True,
        "enabled": body.enabled,
        "message": (
            "Upstox daily approval request ON hai."
            if body.enabled
            else "Upstox daily approval request OFF hai."
        ),
    }


@public_router.post("/notifier")
async def receive_upstox_token(request: Request):
    raw = await request.body()
    if len(raw) > 64 * 1024:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Upstox notifier payload")
    message_type = str(payload.get("message_type") or "access_token").lower()
    if message_type != "access_token":
        raise HTTPException(status_code=400, detail="Unsupported Upstox message type")
    return store_notifier_token(payload)


def request_due_tokens_once(now_utc: datetime | None = None) -> dict:
    now = (now_utc or _utc_now()).astimezone(timezone.utc)
    local = now.astimezone(IST)
    if local.weekday() >= 5 or not (
        REQUEST_START_IST <= local.time() <= REQUEST_END_IST
    ):
        return {"eligible": 0, "requested": 0, "failed": 0, "window_open": False}

    conn = get_db()
    try:
        ensure_upstox_token_schema(conn)
        rows = conn.execute(
            """
            SELECT a.user_id, a.status, a.last_request_date, a.last_request_at,
                   a.last_token_at, a.token_expires_at
            FROM upstox_token_automation a
            JOIN broker_credentials b ON b.user_id=a.user_id
            JOIN users u ON u.id=a.user_id
            WHERE a.enabled=1 AND LOWER(b.broker_name)='upstox'
              AND b.is_active=1 AND COALESCE(u.is_active, 1)=1
            GROUP BY a.user_id
            """
        ).fetchall()
    finally:
        conn.close()

    today = local.date().isoformat()
    due = []
    for row in rows:
        token_expiry = _parse_iso(row["token_expires_at"])
        if token_expiry is not None and token_expiry.astimezone(timezone.utc) > now:
            continue
        last_token = _parse_iso(row["last_token_at"])
        # Older notifier payloads may omit expires_at. A token issued after
        # 03:30 IST today is known to remain valid through today's session.
        if (
            token_expiry is None
            and last_token is not None
            and last_token.astimezone(IST).date().isoformat() == today
            and last_token.astimezone(IST).time() >= wall_time(3, 30)
        ):
            continue
        if (
            str(row["last_request_date"] or "") == today
            and row["status"] == "approval_pending"
        ):
            continue
        last_attempt = _parse_iso(row["last_request_at"])
        if (
            last_attempt is not None
            and (now - last_attempt.astimezone(timezone.utc)).total_seconds()
            < REQUEST_RETRY_SECONDS
        ):
            continue
        due.append(int(row["user_id"]))

    requested = 0
    failed = 0
    for user_id in due:
        try:
            initiate_token_request(user_id)
            requested += 1
        except Exception as exc:
            failed += 1
            print(
                "Upstox daily token request warning | "
                f"user={user_id} | {str(exc)[:160]}"
            )
    return {
        "eligible": len(due),
        "requested": requested,
        "failed": failed,
        "window_open": True,
    }


def schedule_upstox_token_requests() -> bool:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return False
        _scheduler_started = True

    def worker():
        while True:
            try:
                request_due_tokens_once()
            except Exception as exc:
                print(f"Upstox token scheduler warning: {str(exc)[:180]}")
            time.sleep(60)

    threading.Thread(
        target=worker,
        name="okai-upstox-daily-token-request",
        daemon=True,
    ).start()
    return True


__all__ = [
    "enable_for_saved_upstox",
    "ensure_upstox_token_schema",
    "initiate_token_request",
    "notifier_url",
    "public_router",
    "request_due_tokens_once",
    "router",
    "schedule_upstox_token_requests",
    "store_notifier_token",
    "token_status_for_user",
]
