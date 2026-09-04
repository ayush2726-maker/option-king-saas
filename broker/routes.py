from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from datetime import datetime
import threading
import time

from database import get_db
from auth.routes import get_current_user
from auth.utils import encrypt_credential, decrypt_credential
from bot.brokers.factory import (
    create_broker,
    get_all_brokers_info,
    get_broker_info,
    get_supported_brokers,
)
from broker.selection import get_selected_broker


router = APIRouter(prefix="/broker", tags=["Broker"])

_TEST_CACHE = {}
_TEST_CACHE_LOCK = threading.Lock()
_TEST_CACHE_SECONDS = 30


class BrokerConnectRequest(BaseModel):
    broker_name: str
    client_id: str
    api_key: str
    api_secret: str
    totp_secret: str = None


def _cached_test_result(user_id: int, broker_name: str):
    key = (int(user_id), str(broker_name).lower())
    now = time.monotonic()
    with _TEST_CACHE_LOCK:
        cached = _TEST_CACHE.get(key)
        if not cached or cached["expires_at"] <= now:
            _TEST_CACHE.pop(key, None)
            return None
        return {**cached["result"], "cached": True}


def _cache_test_result(user_id: int, broker_name: str, result: dict, ttl=None):
    key = (int(user_id), str(broker_name).lower())
    seconds = max(1, int(ttl or _TEST_CACHE_SECONDS))
    with _TEST_CACHE_LOCK:
        _TEST_CACHE[key] = {
            "expires_at": time.monotonic() + seconds,
            "result": dict(result),
        }


def _invalidate_test_result(user_id: int, broker_name: str):
    key = (int(user_id), str(broker_name).lower())
    with _TEST_CACHE_LOCK:
        _TEST_CACHE.pop(key, None)


def _stop_stale_broker_runtime(user_id: int) -> None:
    """Stop any in-memory session that still belongs to the previous broker.

    Broker switching is deliberately fail-safe: no old PAPER/LIVE loop is
    allowed to continue after another broker becomes selected. The user can
    press Start Bot again and it will bind to the newly selected broker.
    """
    try:
        from bot.angel_fetcher import reset_user_broker_runtime

        reset_user_broker_runtime(int(user_id))
        return
    except Exception:
        pass

    try:
        from bot.angel_fetcher import stop_user_bot

        stop_user_bot(int(user_id))
    except Exception:
        pass


def _mark_bot_stopped_after_switch(conn, user_id: int) -> None:
    for table in ("user_bot_state", "bot_status"):
        try:
            conn.execute(
                f"UPDATE {table} SET is_running=0 WHERE user_id=?",
                (int(user_id),),
            )
        except Exception:
            pass


@router.get("/supported")
def list_supported_brokers():
    return {"success": True, "brokers": get_all_brokers_info()}


@router.get("/info/{broker_name}")
def get_single_broker_info(broker_name: str):
    info = get_broker_info(broker_name)
    if "error" in info:
        raise HTTPException(status_code=404, detail=info["error"])
    return {"success": True, "broker": info}


@router.get("/setup-guide/{broker_name}")
def get_setup_guide(broker_name: str):
    info = get_broker_info(broker_name)
    if "error" in info:
        raise HTTPException(status_code=404, detail=info["error"])
    return {
        "success": True,
        "setup_guide": info["setup_guide"],
        "required_fields": info["required_fields"],
    }


@router.post("/connect")
def connect_broker(req: BrokerConnectRequest, authorization: str = Header(None)):
    user = get_current_user(authorization)
    if user["subscription_status"] not in ("trial", "active"):
        raise HTTPException(status_code=403, detail="Active subscription required")

    broker_name = req.broker_name.lower().strip()
    if broker_name not in get_supported_brokers():
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported broker. Supported: {get_supported_brokers()}",
        )

    # Never save/select credentials unless this exact broker login succeeds.
    try:
        broker = create_broker(
            broker_name,
            req.client_id,
            req.api_key,
            req.api_secret,
            req.totp_secret,
        )
        login_result = broker.login()
        if not login_result.get("success"):
            if login_result.get("rate_limited"):
                retry_after = max(
                    1, int(login_result.get("retry_after_seconds") or 45)
                )
                raise HTTPException(
                    status_code=429,
                    detail=str(login_result.get("message"))[:240],
                    headers={"Retry-After": str(retry_after)},
                )
            raise HTTPException(
                status_code=400,
                detail=(
                    "Broker login failed: "
                    + str(login_result.get("message") or "Unknown login error")[:240]
                ),
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Broker login failed: {str(exc)[:240]}",
        ) from exc

    enc_api_key = encrypt_credential(req.api_key)
    enc_api_secret = encrypt_credential(req.api_secret)
    enc_totp = encrypt_credential(req.totp_secret) if req.totp_secret else None
    now = datetime.utcnow().isoformat()

    conn = get_db()
    try:
        previous = get_selected_broker(conn, user["id"])
        previous_name = (
            str(previous["broker_name"] or "").lower()
            if previous is not None
            else None
        )

        # Single source of truth: connecting/saving one broker deselects all
        # others before the selected row is activated.
        conn.execute(
            "UPDATE broker_credentials SET is_active=0 WHERE user_id=?",
            (user["id"],),
        )

        existing = conn.execute(
            """SELECT id FROM broker_credentials
               WHERE user_id=? AND broker_name=?""",
            (user["id"], broker_name),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE broker_credentials
                   SET client_id=?, api_key=?, api_secret=?, totp_secret=?,
                       is_active=1, last_connected=?
                   WHERE id=?""",
                (
                    req.client_id,
                    enc_api_key,
                    enc_api_secret,
                    enc_totp,
                    now,
                    existing["id"],
                ),
            )
            msg = f"{broker_name.title()} credentials updated and selected"
        else:
            conn.execute(
                """INSERT INTO broker_credentials
                   (user_id, broker_name, client_id, api_key, api_secret,
                    totp_secret, is_active, last_connected)
                   VALUES (?,?,?,?,?,?,1,?)""",
                (
                    user["id"],
                    broker_name,
                    req.client_id,
                    enc_api_key,
                    enc_api_secret,
                    enc_totp,
                    now,
                ),
            )
            msg = f"{broker_name.title()} connected and selected successfully"

        _mark_bot_stopped_after_switch(conn, user["id"])
        conn.commit()
    finally:
        conn.close()

    # Clear the previous broker's live/paper loop and quote session. This runs
    # even when credentials for the same broker are refreshed, so new tokens are
    # guaranteed to be used on the next Start Bot.
    _stop_stale_broker_runtime(user["id"])
    _invalidate_test_result(user["id"], broker_name)

    if broker_name == "upstox":
        # Keep the existing daily-approval setup enrolled, but do not make the
        # normal broker-list polling path depend on automation DB work.
        try:
            from broker.upstox_token_automation import enable_for_saved_upstox

            enable_for_saved_upstox(user["id"], True)
        except Exception:
            pass

    return {
        "success": True,
        "message": msg + ". Start Bot dobara dabayein.",
        "broker": broker_name,
        "selected_broker": broker_name,
        "previous_broker": previous_name,
        "client_id": req.client_id,
        "runtime_rebind_required": True,
    }


@router.get("/list")
def list_brokers(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, broker_name, client_id, is_active,
                      last_connected, created_at
               FROM broker_credentials
               WHERE user_id=?
               ORDER BY is_active DESC, last_connected DESC, id DESC""",
            (user["id"],),
        ).fetchall()
        selected = get_selected_broker(conn, user["id"])
    finally:
        conn.close()

    brokers = []
    for row in rows:
        item = dict(row)
        item["selected"] = bool(item.get("is_active"))
        brokers.append(item)

    return {
        "success": True,
        "selected_broker": (
            str(selected["broker_name"] or "").lower()
            if selected is not None
            else None
        ),
        "brokers": brokers,
    }


@router.get("/test/{broker_name}")
def test_broker_connection(broker_name: str, authorization: str = Header(None)):
    user = get_current_user(authorization)
    requested = broker_name.lower().strip()
    cached = _cached_test_result(user["id"], requested)
    if cached is not None:
        return cached

    conn = get_db()
    try:
        cred = conn.execute(
            """SELECT * FROM broker_credentials
               WHERE user_id=? AND broker_name=?""",
            (user["id"], requested),
        ).fetchone()
        selected = get_selected_broker(conn, user["id"])
    finally:
        conn.close()

    if not cred:
        raise HTTPException(status_code=404, detail="Broker not connected")

    api_key = decrypt_credential(cred["api_key"])
    api_secret = decrypt_credential(cred["api_secret"])
    totp = decrypt_credential(cred["totp_secret"]) if cred["totp_secret"] else None

    try:
        broker = create_broker(
            requested,
            cred["client_id"],
            api_key,
            api_secret,
            totp,
        )
        result = broker.login()
        if result.get("success"):
            funds = broker.get_funds()
            response = {
                "success": True,
                "broker": requested,
                "selected": bool(
                    selected is not None
                    and str(selected["broker_name"] or "").lower() == requested
                ),
                "status": "connected",
                "funds": funds,
            }
            if funds.get("rate_limited"):
                response["warning"] = funds.get("message")
                response["retry_after_seconds"] = funds.get(
                    "retry_after_seconds", 45
                )
            _cache_test_result(user["id"], requested, response)
            return response

        response = {
            "success": False,
            "broker": requested,
            "status": result.get("status") or "auth_failed",
            "message": result.get("message"),
            "error_code": result.get("error_code"),
            "rate_limited": bool(result.get("rate_limited")),
            "retry_after_seconds": result.get("retry_after_seconds"),
        }
        if response["rate_limited"]:
            _cache_test_result(
                user["id"],
                requested,
                response,
                response["retry_after_seconds"] or 45,
            )
        return response
    except Exception as exc:
        return {
            "success": False,
            "broker": requested,
            "status": "error",
            "message": str(exc),
        }


@router.delete("/disconnect/{broker_name}")
def disconnect_broker(broker_name: str, authorization: str = Header(None)):
    user = get_current_user(authorization)
    requested = broker_name.lower().strip()
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT id, is_active FROM broker_credentials
               WHERE user_id=? AND broker_name=?""",
            (user["id"], requested),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Broker not found")

        was_selected = bool(row["is_active"])
        conn.execute(
            "DELETE FROM broker_credentials WHERE id=?",
            (row["id"],),
        )
        if was_selected:
            _mark_bot_stopped_after_switch(conn, user["id"])
        conn.commit()
    finally:
        conn.close()

    if was_selected:
        _stop_stale_broker_runtime(user["id"])
    _invalidate_test_result(user["id"], requested)

    return {
        "success": True,
        "message": f"{requested} disconnected",
        "selected_broker": None if was_selected else None,
        "runtime_stopped": was_selected,
    }
