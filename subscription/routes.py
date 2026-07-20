from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
from math import ceil
from urllib.parse import quote, urlencode
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

import requests

from database import get_db
from auth.routes import get_current_user


router = APIRouter(prefix="/subscription", tags=["Subscription"])

PLAN_ID = "monthly_1999"
PLAN = {
    "id": PLAN_ID,
    "name": "OKAI Monthly Plan",
    "price": 199900,
    "amount_rupees": 1999,
    "display_price": "₹1,999",
    "duration_days": 30,
    "renewal": "manual",
    "features": [
        "Full Option King AI access",
        "Paper and Live trading tools",
        "Strategy builder and backtests",
        "Trade alerts and reports",
        "30 days validity",
    ],
}

_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,63}$")
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {"access_token": "", "expires_at": 0}


def _utcnow():
    return datetime.utcnow()


def _phonepe_environment():
    value = str(os.getenv("PHONEPE_ENV", "sandbox")).strip().lower()
    return "production" if value in {"prod", "production", "live"} else "sandbox"


def _phonepe_endpoints():
    if _phonepe_environment() == "production":
        return {
            "auth": "https://api.phonepe.com/apis/identity-manager/v1/oauth/token",
            "api": "https://api.phonepe.com/apis/pg",
        }
    return {
        "auth": "https://api-preprod.phonepe.com/apis/pg-sandbox/v1/oauth/token",
        "api": "https://api-preprod.phonepe.com/apis/pg-sandbox",
    }


def _phonepe_credentials():
    return {
        "client_id": str(os.getenv("PHONEPE_CLIENT_ID", "")).strip(),
        "client_secret": str(os.getenv("PHONEPE_CLIENT_SECRET", "")).strip(),
        "client_version": str(os.getenv("PHONEPE_CLIENT_VERSION", "1")).strip(),
        "merchant_id": str(os.getenv("PHONEPE_MERCHANT_ID", "")).strip(),
    }


def _phonepe_configured():
    credentials = _phonepe_credentials()
    return bool(
        credentials["client_id"]
        and credentials["client_secret"]
        and credentials["client_version"]
    )


def _redirect_base_url():
    return str(
        os.getenv(
            "PHONEPE_REDIRECT_BASE_URL",
            "https://option-king-saas-production.up.railway.app/subscription/phonepe/return",
        )
    ).strip()


def _safe_json(value):
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)[:12000]
    except Exception:
        return "{}"


def _ensure_subscription_schema():
    conn = get_db()
    try:
        additions = [
            ("payment_gateway", "TEXT"),
            ("merchant_order_id", "TEXT"),
            ("gateway_order_id", "TEXT"),
            ("gateway_transaction_id", "TEXT"),
            ("gateway_state", "TEXT"),
            ("gateway_payload", "TEXT"),
            ("checkout_url", "TEXT"),
            ("activated_at", "TEXT"),
            ("updated_at", "TEXT"),
        ]
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(subscriptions)").fetchall()
        }
        for name, sql_type in additions:
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE subscriptions ADD COLUMN {name} {sql_type}"
                )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_merchant_order
            ON subscriptions(merchant_order_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_subscriptions_user_status
            ON subscriptions(user_id, status, created_at DESC)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _phonepe_access_token(force_refresh=False):
    if not _phonepe_configured():
        raise HTTPException(
            status_code=503,
            detail="PhonePe merchant credentials are not configured yet",
        )

    now = int(time.time())
    with _TOKEN_LOCK:
        if (
            not force_refresh
            and _TOKEN_CACHE["access_token"]
            and int(_TOKEN_CACHE["expires_at"] or 0) > now + 60
        ):
            return _TOKEN_CACHE["access_token"]

        credentials = _phonepe_credentials()
        response = requests.post(
            _phonepe_endpoints()["auth"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": credentials["client_id"],
                "client_version": credentials["client_version"],
                "client_secret": credentials["client_secret"],
                "grant_type": "client_credentials",
            },
            timeout=20,
        )
        try:
            data = response.json()
        except Exception:
            data = {}
        if not response.ok or not data.get("access_token"):
            message = data.get("message") or data.get("error_description") or response.text
            raise HTTPException(
                status_code=502,
                detail=f"PhonePe authorization failed: {str(message)[:180]}",
            )

        expires_at = int(data.get("expires_at") or now + 300)
        _TOKEN_CACHE["access_token"] = str(data["access_token"])
        _TOKEN_CACHE["expires_at"] = expires_at
        return _TOKEN_CACHE["access_token"]


def _phonepe_headers(force_refresh=False):
    return {
        "Content-Type": "application/json",
        "Authorization": f"O-Bearer {_phonepe_access_token(force_refresh)}",
    }


def _phonepe_request(method, url, **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    headers.update(_phonepe_headers(False))
    response = requests.request(method, url, headers=headers, timeout=25, **kwargs)
    if response.status_code == 401:
        headers.update(_phonepe_headers(True))
        response = requests.request(method, url, headers=headers, timeout=25, **kwargs)
    try:
        data = response.json()
    except Exception:
        data = {}
    if not response.ok:
        message = data.get("message") or data.get("code") or response.text
        raise HTTPException(
            status_code=502,
            detail=f"PhonePe gateway error: {str(message)[:180]}",
        )
    return data


def _validate_merchant_order_id(value):
    order_id = str(value or "").strip()
    if not _ORDER_ID_RE.fullmatch(order_id):
        raise HTTPException(status_code=400, detail="Invalid payment order ID")
    return order_id


def _new_merchant_order_id(user_id):
    timestamp = int(time.time())
    suffix = secrets.token_hex(5).upper()
    return f"OKAI_{int(user_id)}_{timestamp}_{suffix}"[:63]


def _normalise_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        return "+91" + digits
    if 11 <= len(digits) <= 15:
        return "+" + digits
    return ""


def _completed_transaction_id(payload):
    for payment in reversed(payload.get("paymentDetails") or []):
        if str(payment.get("state", "")).upper() == "COMPLETED":
            return str(payment.get("transactionId") or "")
    return ""


def _activate_subscription(conn, subscription_row, phonepe_payload):
    if str(subscription_row["status"] or "").lower() == "active":
        return {
            "activated": False,
            "valid_till": subscription_row["valid_till"],
        }

    expected_amount = int(PLAN["price"])
    actual_amount = int(phonepe_payload.get("amount") or 0)
    state = str(phonepe_payload.get("state") or "").upper()
    if state != "COMPLETED" or actual_amount != expected_amount:
        raise HTTPException(
            status_code=400,
            detail="Payment is not completed or the verified amount is incorrect",
        )

    now = _utcnow()
    base_date = now
    current = conn.execute(
        """
        SELECT id, valid_till FROM subscriptions
        WHERE user_id=? AND status='active' AND id<>?
          AND valid_till IS NOT NULL
        ORDER BY datetime(valid_till) DESC LIMIT 1
        """,
        (subscription_row["user_id"], subscription_row["id"]),
    ).fetchone()
    if current and current["valid_till"]:
        try:
            current_till = datetime.fromisoformat(current["valid_till"])
            if current_till > now:
                base_date = current_till
        except Exception:
            pass

    valid_from = now
    valid_till = base_date + timedelta(days=PLAN["duration_days"])
    transaction_id = _completed_transaction_id(phonepe_payload)
    payload_json = _safe_json(phonepe_payload)

    cursor = conn.execute(
        """
        UPDATE subscriptions
        SET status='active', payment_gateway='phonepe',
            gateway_transaction_id=?, gateway_state='COMPLETED',
            gateway_payload=?, valid_from=?, valid_till=?,
            activated_at=?, updated_at=?
        WHERE id=? AND status<>'active'
        """,
        (
            transaction_id,
            payload_json,
            valid_from.isoformat(),
            valid_till.isoformat(),
            now.isoformat(),
            now.isoformat(),
            subscription_row["id"],
        ),
    )
    if cursor.rowcount != 1:
        fresh = conn.execute(
            "SELECT valid_till FROM subscriptions WHERE id=?",
            (subscription_row["id"],),
        ).fetchone()
        return {
            "activated": False,
            "valid_till": fresh["valid_till"] if fresh else None,
        }

    conn.execute(
        "UPDATE users SET subscription_status='active' WHERE id=?",
        (subscription_row["user_id"],),
    )
    conn.commit()
    return {"activated": True, "valid_till": valid_till.isoformat()}


def _phonepe_order_status(merchant_order_id):
    order_id = _validate_merchant_order_id(merchant_order_id)
    url = (
        f"{_phonepe_endpoints()['api']}/checkout/v2/order/"
        f"{quote(order_id, safe='')}/status?details=true&errorContext=true"
    )
    return _phonepe_request("GET", url)


def _sync_order(merchant_order_id, expected_user_id=None):
    _ensure_subscription_schema()
    order_id = _validate_merchant_order_id(merchant_order_id)
    conn = get_db()
    try:
        query = "SELECT * FROM subscriptions WHERE merchant_order_id=?"
        params = [order_id]
        if expected_user_id is not None:
            query += " AND user_id=?"
            params.append(int(expected_user_id))
        subscription_row = conn.execute(query, tuple(params)).fetchone()
        if not subscription_row:
            raise HTTPException(status_code=404, detail="Payment order not found")

        if str(subscription_row["status"] or "").lower() == "active":
            return {
                "success": True,
                "state": "COMPLETED",
                "subscription_active": True,
                "valid_till": subscription_row["valid_till"],
                "merchant_order_id": order_id,
            }

        status_payload = _phonepe_order_status(order_id)
        state = str(status_payload.get("state") or "PENDING").upper()
        amount = int(status_payload.get("amount") or 0)
        now = _utcnow().isoformat()

        if amount and amount != int(PLAN["price"]):
            conn.execute(
                """
                UPDATE subscriptions
                SET status='failed', gateway_state='AMOUNT_MISMATCH',
                    gateway_payload=?, updated_at=?
                WHERE id=?
                """,
                (_safe_json(status_payload), now, subscription_row["id"]),
            )
            conn.commit()
            raise HTTPException(status_code=400, detail="Verified payment amount mismatch")

        if state == "COMPLETED":
            activation = _activate_subscription(conn, subscription_row, status_payload)
            return {
                "success": True,
                "state": state,
                "subscription_active": True,
                "valid_till": activation["valid_till"],
                "merchant_order_id": order_id,
            }

        next_status = "failed" if state == "FAILED" else "pending"
        conn.execute(
            """
            UPDATE subscriptions
            SET status=?, gateway_state=?, gateway_payload=?, updated_at=?
            WHERE id=? AND status<>'active'
            """,
            (
                next_status,
                state,
                _safe_json(status_payload),
                now,
                subscription_row["id"],
            ),
        )
        conn.commit()
        return {
            "success": True,
            "state": state,
            "subscription_active": False,
            "merchant_order_id": order_id,
        }
    finally:
        conn.close()


def _background_sync_order(merchant_order_id):
    try:
        _sync_order(merchant_order_id)
    except Exception as exc:
        print(f"PHONEPE WEBHOOK SYNC FAILED | {merchant_order_id} | {str(exc)[:180]}")


@router.get("/plans")
def get_plans():
    return {
        "success": True,
        "plans": {PLAN_ID: PLAN},
        "recommended_plan": PLAN_ID,
        "gateway": "phonepe",
        "upi_supported": True,
        "trial": {
            "duration_days": 7,
            "description": "Full access for 7 days — no payment required",
        },
    }


@router.get("/phonepe/config")
def phonepe_config(authorization: str = Header(None)):
    get_current_user(authorization)
    return {
        "success": True,
        "available": _phonepe_configured(),
        "environment": _phonepe_environment(),
        "plan": PLAN,
        "renewal": "manual_every_30_days",
        "upi_supported": True,
    }


@router.post("/phonepe/create-order")
def phonepe_create_order(body: dict = None, authorization: str = Header(None)):
    _ensure_subscription_schema()
    user = get_current_user(authorization)
    if not _phonepe_configured():
        raise HTTPException(
            status_code=503,
            detail="PhonePe merchant account is not configured yet",
        )

    merchant_order_id = _new_merchant_order_id(user["id"])
    redirect_query = urlencode({"merchantOrderId": merchant_order_id})
    separator = "&" if "?" in _redirect_base_url() else "?"
    redirect_url = f"{_redirect_base_url()}{separator}{redirect_query}"

    request_body = {
        "merchantOrderId": merchant_order_id,
        "amount": int(PLAN["price"]),
        "expireAfter": 1200,
        "paymentFlow": {
            "type": "PG_CHECKOUT",
            "merchantUrls": {"redirectUrl": redirect_url},
        },
        "disablePaymentRetry": False,
        "metaInfo": {
            "udf1": str(user["id"]),
            "udf2": PLAN_ID,
            "udf3": str(user["email"] or "")[:200],
            "udf11": "OKAI_MONTHLY_1999",
        },
    }
    phone = _normalise_phone(user["phone"] if "phone" in user.keys() else "")
    if phone:
        request_body["prefillUserLoginDetails"] = {"phoneNumber": phone}

    url = f"{_phonepe_endpoints()['api']}/checkout/v2/pay"
    response = _phonepe_request("POST", url, json=request_body)
    checkout_url = str(response.get("redirectUrl") or "").strip()
    if not checkout_url:
        raise HTTPException(status_code=502, detail="PhonePe checkout URL was not returned")

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO subscriptions(
                user_id, plan, amount, status, payment_gateway,
                merchant_order_id, gateway_order_id, gateway_state,
                gateway_payload, checkout_url, updated_at
            ) VALUES (?, ?, ?, 'pending', 'phonepe', ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                PLAN_ID,
                float(PLAN["amount_rupees"]),
                merchant_order_id,
                str(response.get("orderId") or ""),
                str(response.get("state") or "PENDING"),
                _safe_json(response),
                checkout_url,
                _utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "gateway": "phonepe",
        "merchant_order_id": merchant_order_id,
        "checkout_url": checkout_url,
        "amount": PLAN["price"],
        "display_price": PLAN["display_price"],
        "currency": "INR",
        "plan": PLAN,
        "expires_in_seconds": 1200,
    }


@router.get("/phonepe/status/{merchant_order_id}")
def phonepe_payment_status(
    merchant_order_id: str,
    authorization: str = Header(None),
):
    user = get_current_user(authorization)
    return _sync_order(merchant_order_id, expected_user_id=user["id"])


@router.get("/phonepe/return", response_class=HTMLResponse)
def phonepe_return(merchantOrderId: str = ""):
    state = "PENDING"
    active = False
    message = "Payment verification is still pending. Return to the app and tap Check Payment Status."
    try:
        result = _sync_order(merchantOrderId)
        state = result.get("state", "PENDING")
        active = bool(result.get("subscription_active"))
        if active:
            message = "Payment verified. Your 30-day OKAI subscription is active. Return to the app."
        elif state == "FAILED":
            message = "Payment failed. Return to the app and try again."
    except Exception:
        message = "Return to the OKAI app and tap Check Payment Status."

    color = "#00a884" if active else "#e5a000" if state == "PENDING" else "#d93025"
    safe_message = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return HTMLResponse(
        f"""
        <!doctype html>
        <html><head><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>OKAI Payment</title></head>
        <body style="font-family:Arial,sans-serif;background:#0a0a0f;color:#e8e8f0;padding:28px;text-align:center">
          <div style="max-width:520px;margin:40px auto;background:#13131f;border:1px solid #252540;border-radius:18px;padding:28px">
            <div style="font-size:42px">{'✅' if active else '⏳' if state == 'PENDING' else '❌'}</div>
            <h2 style="color:{color}">Payment {state.title()}</h2>
            <p style="line-height:1.6">{safe_message}</p>
            <p style="color:#9090ad;font-size:13px">You may close this page and reopen Option King AI.</p>
          </div>
        </body></html>
        """
    )


def _valid_phonepe_webhook_auth(authorization):
    username = str(os.getenv("PHONEPE_WEBHOOK_USERNAME", "")).strip()
    password = str(os.getenv("PHONEPE_WEBHOOK_PASSWORD", "")).strip()
    if not username or not password:
        return False
    expected = hashlib.sha256(f"{username}:{password}".encode()).hexdigest()
    candidate = str(authorization or "").strip().lower()
    for prefix in ("sha256 ", "sha256=", "bearer "):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):].strip()
    return hmac.compare_digest(expected.lower(), candidate.lower())


@router.post("/phonepe/webhook")
async def phonepe_webhook(request: Request, background_tasks: BackgroundTasks):
    authorization = request.headers.get("Authorization", "")
    if not _valid_phonepe_webhook_auth(authorization):
        raise HTTPException(status_code=401, detail="Invalid PhonePe webhook authorization")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    event = str(data.get("event") or "")
    payload = data.get("payload") or {}
    merchant_order_id = str(
        payload.get("merchantOrderId")
        or payload.get("merchant_order_id")
        or data.get("merchantOrderId")
        or ""
    ).strip()
    if merchant_order_id and event in {"checkout.order.completed", "checkout.order.failed"}:
        background_tasks.add_task(_background_sync_order, merchant_order_id)
    return {"status": "accepted"}


@router.get("/status")
def get_subscription_status(authorization: str = Header(None)):
    _ensure_subscription_schema()
    user = get_current_user(authorization)
    now = _utcnow()

    if bool(user["is_admin"]):
        conn = get_db()
        try:
            conn.execute(
                """
                UPDATE users
                SET subscription_status='active', trial_ends_at=NULL
                WHERE id=?
                """,
                (user["id"],),
            )
            history = conn.execute(
                """
                SELECT plan, amount, status, payment_gateway, merchant_order_id,
                       gateway_state, valid_from, valid_till, created_at
                FROM subscriptions WHERE user_id=? ORDER BY created_at DESC
                """,
                (user["id"],),
            ).fetchall()
            conn.commit()
        finally:
            conn.close()

        return {
            "success": True,
            "subscription_status": "active",
            "days_remaining": None,
            "unlimited": True,
            "is_admin": True,
            "active_subscription": {
                "plan": "admin_unlimited",
                "status": "active",
                "valid_from": None,
                "valid_till": None,
            },
            "history": [dict(row) for row in history],
            "plan": PLAN,
        }

    conn = get_db()
    try:
        expired = conn.execute(
            """
            SELECT id FROM subscriptions
            WHERE user_id=? AND status='active' AND valid_till IS NOT NULL
              AND datetime(valid_till) <= datetime(?)
            """,
            (user["id"], now.isoformat()),
        ).fetchall()
        if expired:
            conn.execute(
                """
                UPDATE subscriptions SET status='expired', updated_at=?
                WHERE user_id=? AND status='active' AND valid_till IS NOT NULL
                  AND datetime(valid_till) <= datetime(?)
                """,
                (now.isoformat(), user["id"], now.isoformat()),
            )

        active_sub = conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE user_id=? AND status='active' AND valid_till IS NOT NULL
              AND datetime(valid_till) > datetime(?)
            ORDER BY datetime(valid_till) DESC LIMIT 1
            """,
            (user["id"], now.isoformat()),
        ).fetchone()

        if active_sub:
            conn.execute(
                "UPDATE users SET subscription_status='active' WHERE id=?",
                (user["id"],),
            )
            subscription_status = "active"
        else:
            trial_active = False
            if user["trial_ends_at"]:
                try:
                    trial_active = datetime.fromisoformat(user["trial_ends_at"]) > now
                except Exception:
                    trial_active = False
            subscription_status = "trial" if trial_active else "expired"
            conn.execute(
                "UPDATE users SET subscription_status=? WHERE id=?",
                (subscription_status, user["id"]),
            )

        history = conn.execute(
            """
            SELECT plan, amount, status, payment_gateway, merchant_order_id,
                   gateway_state, valid_from, valid_till, created_at
            FROM subscriptions WHERE user_id=? ORDER BY created_at DESC
            """,
            (user["id"],),
        ).fetchall()
        conn.commit()
    finally:
        conn.close()

    days_remaining = 0
    if active_sub and active_sub["valid_till"]:
        seconds = (datetime.fromisoformat(active_sub["valid_till"]) - now).total_seconds()
        days_remaining = max(0, ceil(seconds / 86400))
    elif subscription_status == "trial" and user["trial_ends_at"]:
        seconds = (datetime.fromisoformat(user["trial_ends_at"]) - now).total_seconds()
        days_remaining = max(0, ceil(seconds / 86400))

    return {
        "success": True,
        "subscription_status": subscription_status,
        "days_remaining": days_remaining,
        "active_subscription": dict(active_sub) if active_sub else None,
        "history": [dict(row) for row in history],
        "plan": PLAN,
    }


# Backward-compatible aliases used by older mobile builds.
@router.post("/create-order")
def create_order_compat(body: dict = None, authorization: str = Header(None)):
    return phonepe_create_order(body=body or {}, authorization=authorization)


@router.post("/verify-payment")
def verify_payment_compat(body: dict, authorization: str = Header(None)):
    merchant_order_id = body.get("merchant_order_id") or body.get("merchantOrderId")
    if not merchant_order_id:
        raise HTTPException(status_code=400, detail="Missing merchant order ID")
    user = get_current_user(authorization)
    return _sync_order(merchant_order_id, expected_user_id=user["id"])
