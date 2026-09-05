from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import urlencode
import json
import os
import re
import secrets
import time

import requests
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from auth.routes import get_current_user
from database import get_db


router = APIRouter(prefix="/paytm", tags=["Subscription - Paytm"])
_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SUCCESS_STATES = {"SUCCESS", "TXN_SUCCESS", "COMPLETED", "PAID"}


def _shared_plan():
    from subscription.routes import PLAN, PLAN_ID, _ensure_subscription_schema

    _ensure_subscription_schema()
    return PLAN_ID, PLAN


def _utcnow():
    return datetime.utcnow()


def _safe_json(value):
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)[:12000]
    except Exception:
        return "{}"


def _paytm_environment():
    value = str(os.getenv("PAYTM_ENV", "staging")).strip().lower()
    return "production" if value in {"prod", "production", "live"} else "staging"


def _paytm_endpoints():
    if _paytm_environment() == "production":
        base = str(
            os.getenv("PAYTM_API_BASE_URL", "https://secure.paytmpayments.com")
        ).rstrip("/")
    else:
        base = str(
            os.getenv("PAYTM_API_BASE_URL", "https://securestage.paytmpayments.com")
        ).rstrip("/")
    return {
        "create_link": f"{base}/link/create",
        "fetch_transactions": f"{base}/link/fetchTransaction",
    }


def _paytm_credentials():
    return {
        "mid": str(os.getenv("PAYTM_MID", "")).strip(),
        "merchant_key": str(os.getenv("PAYTM_MERCHANT_KEY", "")).strip(),
        "client_id": str(os.getenv("PAYTM_CLIENT_ID", "")).strip(),
    }


def _checksum_module():
    try:
        import PaytmChecksum

        return PaytmChecksum
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Paytm checksum utility is unavailable: {str(exc)[:120]}",
        )


def _paytm_configured():
    credentials = _paytm_credentials()
    if not credentials["mid"] or not credentials["merchant_key"]:
        return False
    try:
        import PaytmChecksum  # noqa: F401

        return True
    except Exception:
        return False


def _callback_base_url():
    return str(
        os.getenv(
            "PAYTM_LINK_CALLBACK_URL",
            "https://option-king-saas-production.up.railway.app/subscription/paytm/webhook",
        )
    ).strip()


def _return_base_url():
    return str(
        os.getenv(
            "PAYTM_LINK_RETURN_URL",
            "https://option-king-saas-production.up.railway.app/subscription/paytm/return",
        )
    ).strip()


def _url_with_order(base_url, merchant_request_id):
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'merchantRequestId': merchant_request_id})}"


def _new_merchant_request_id(user_id):
    value = f"OKAI_PTM_{int(user_id)}_{int(time.time())}_{secrets.token_hex(4).upper()}"
    return value[:64]


def _validate_order_id(value):
    order_id = str(value or "").strip()
    if not _ORDER_ID_RE.fullmatch(order_id):
        raise HTTPException(status_code=400, detail="Invalid Paytm payment order ID")
    return order_id


def _normalise_mobile(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("91"):
        return digits[-10:]
    return digits if len(digits) == 10 else ""


def _json_body(body):
    return json.dumps(body, ensure_ascii=False)


def _verify_json_signature(body, signature):
    if not signature:
        return False
    credentials = _paytm_credentials()
    return bool(
        _checksum_module().verifySignature(
            _json_body(body), credentials["merchant_key"], str(signature)
        )
    )


def _paytm_request(url, body):
    credentials = _paytm_credentials()
    checksum = _checksum_module().generateSignature(
        _json_body(body), credentials["merchant_key"]
    )
    head = {
        "tokenType": "AES",
        "signature": checksum,
        "timestamp": str(int(time.time())),
        "channelId": "WAP",
        "version": "v2",
    }
    if credentials["client_id"]:
        head["clientId"] = credentials["client_id"]

    response = requests.post(
        url,
        json={"body": body, "head": head},
        headers={"Content-Type": "application/json"},
        timeout=25,
    )
    try:
        data = response.json()
    except Exception:
        data = {}

    if not response.ok:
        raise HTTPException(
            status_code=502,
            detail=f"Paytm gateway HTTP error: {str(response.text)[:180]}",
        )

    response_body = data.get("body") or {}
    response_head = data.get("head") or {}
    response_signature = response_head.get("signature")
    if response_signature and not _verify_json_signature(response_body, response_signature):
        raise HTTPException(status_code=502, detail="Invalid Paytm response signature")

    result_info = response_body.get("resultInfo") or data.get("resultInfo") or {}
    result_status = str(result_info.get("resultStatus") or "").upper()
    if result_status and result_status not in {"SUCCESS", "S"}:
        message = (
            result_info.get("resultMessage")
            or result_info.get("resultMsg")
            or result_info.get("resultCode")
            or "Paytm request failed"
        )
        raise HTTPException(status_code=502, detail=str(message)[:180])
    return data


def _decimal_amount(value):
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def _expected_amount():
    _, plan = _shared_plan()
    return Decimal(str(plan["amount_rupees"])).quantize(Decimal("0.01"))


def _find_subscription(conn, merchant_request_id="", link_reference="", user_id=None):
    row = None
    if merchant_request_id:
        query = "SELECT * FROM subscriptions WHERE merchant_order_id=?"
        params = [merchant_request_id]
        if user_id is not None:
            query += " AND user_id=?"
            params.append(int(user_id))
        row = conn.execute(query, tuple(params)).fetchone()
    if row or not link_reference:
        return row

    reference = str(link_reference).strip()
    candidates = {reference}
    if reference.upper().startswith("LI_"):
        candidates.add(reference[3:])
    else:
        candidates.add(f"LI_{reference}")
    placeholders = ",".join("?" for _ in candidates)
    query = f"SELECT * FROM subscriptions WHERE gateway_order_id IN ({placeholders})"
    params = list(candidates)
    if user_id is not None:
        query += " AND user_id=?"
        params.append(int(user_id))
    query += " ORDER BY id DESC LIMIT 1"
    return conn.execute(query, tuple(params)).fetchone()


def _activate_subscription(conn, subscription_row, payload, txn_id, actual_amount):
    if str(subscription_row["status"] or "").lower() == "active":
        return {
            "activated": False,
            "valid_till": subscription_row["valid_till"],
        }

    expected = _expected_amount()
    if _decimal_amount(actual_amount) != expected:
        raise HTTPException(status_code=400, detail="Verified Paytm amount mismatch")

    _, plan = _shared_plan()
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

    valid_till = base_date + timedelta(days=int(plan["duration_days"]))
    cursor = conn.execute(
        """
        UPDATE subscriptions
        SET status='active', payment_gateway='paytm_link',
            gateway_transaction_id=?, gateway_state='TXN_SUCCESS',
            gateway_payload=?, valid_from=?, valid_till=?,
            activated_at=?, updated_at=?
        WHERE id=? AND status<>'active'
        """,
        (
            str(txn_id or ""),
            _safe_json(payload),
            now.isoformat(),
            valid_till.isoformat(),
            now.isoformat(),
            now.isoformat(),
            subscription_row["id"],
        ),
    )
    if cursor.rowcount == 1:
        conn.execute(
            "UPDATE users SET subscription_status='active' WHERE id=?",
            (subscription_row["user_id"],),
        )
        conn.commit()
        return {"activated": True, "valid_till": valid_till.isoformat()}

    fresh = conn.execute(
        "SELECT valid_till FROM subscriptions WHERE id=?", (subscription_row["id"],)
    ).fetchone()
    return {"activated": False, "valid_till": fresh["valid_till"] if fresh else None}


def _normalise_orders(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        if any(key in value for key in ("txnId", "orderId", "orderStatus")):
            return [value]
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def _fetch_link_transactions(link_id):
    credentials = _paytm_credentials()
    return _paytm_request(
        _paytm_endpoints()["fetch_transactions"],
        {
            "mid": credentials["mid"],
            "linkId": str(link_id),
            "pageNo": 1,
            "pageSize": 50,
            "fetchAllTxns": True,
        },
    )


def _sync_order(merchant_request_id, expected_user_id=None):
    order_id = _validate_order_id(merchant_request_id)
    _shared_plan()
    conn = get_db()
    try:
        subscription_row = _find_subscription(
            conn, merchant_request_id=order_id, user_id=expected_user_id
        )
        if not subscription_row:
            raise HTTPException(status_code=404, detail="Paytm payment order not found")

        if str(subscription_row["status"] or "").lower() == "active":
            return {
                "success": True,
                "state": "COMPLETED",
                "subscription_active": True,
                "valid_till": subscription_row["valid_till"],
                "merchant_order_id": order_id,
                "gateway": "paytm_link",
            }

        link_id = str(subscription_row["gateway_order_id"] or "").strip()
        if not link_id:
            raise HTTPException(status_code=409, detail="Paytm link ID is missing")

        response = _fetch_link_transactions(link_id)
        response_body = response.get("body") or {}
        orders = _normalise_orders(response_body.get("orders") or response.get("orders"))
        successful = None
        for item in reversed(orders):
            state = str(item.get("orderStatus") or item.get("status") or "").upper()
            if state in _SUCCESS_STATES:
                successful = item
                break

        now = _utcnow().isoformat()
        if successful:
            actual_amount = successful.get("txnAmount") or successful.get("TXNAMOUNT")
            activation = _activate_subscription(
                conn,
                subscription_row,
                successful,
                successful.get("txnId") or successful.get("TXNID"),
                actual_amount,
            )
            return {
                "success": True,
                "state": "COMPLETED",
                "subscription_active": True,
                "valid_till": activation["valid_till"],
                "merchant_order_id": order_id,
                "gateway": "paytm_link",
            }

        conn.execute(
            """
            UPDATE subscriptions
            SET status='pending', gateway_state='PENDING',
                gateway_payload=?, updated_at=?
            WHERE id=? AND status<>'active'
            """,
            (_safe_json(response), now, subscription_row["id"]),
        )
        conn.commit()
        return {
            "success": True,
            "state": "PENDING",
            "subscription_active": False,
            "merchant_order_id": order_id,
            "gateway": "paytm_link",
        }
    finally:
        conn.close()


@router.get("/config")
def paytm_config(authorization: str = Header(None)):
    get_current_user(authorization)
    _, plan = _shared_plan()
    return {
        "success": True,
        "available": _paytm_configured(),
        "environment": _paytm_environment(),
        "gateway": "paytm_link",
        "plan": plan,
        "renewal": "manual_every_30_days",
    }


@router.post("/create-link")
def create_paytm_link(body: dict = None, authorization: str = Header(None)):
    user = get_current_user(authorization)
    plan_id, plan = _shared_plan()
    if not _paytm_configured():
        raise HTTPException(
            status_code=503,
            detail="Paytm merchant MID/key are not configured yet",
        )

    credentials = _paytm_credentials()
    merchant_request_id = _new_merchant_request_id(user["id"])
    customer_contact = {
        "customerName": str(user["name"] or "OKAI User")[:64],
        "customerEmail": str(user["email"] or "")[:100],
        "customerId": str(user["id"]),
    }
    phone = _normalise_mobile(user["phone"] if "phone" in user.keys() else "")
    if phone:
        customer_contact["customerMobile"] = phone
    customer_contact = {key: value for key, value in customer_contact.items() if value}

    request_body = {
        "merchantRequestId": merchant_request_id,
        "mid": credentials["mid"],
        "linkName": "OKAI Monthly Plan",
        "linkDescription": "OKAI 30 Day Plan",
        "linkType": "FIXED",
        "amount": float(Decimal(str(plan["amount_rupees"]))),
        "sendSms": False,
        "sendEmail": False,
        "customerContact": customer_contact,
        "statusCallbackUrl": _url_with_order(_callback_base_url(), merchant_request_id),
        "maxPaymentsAllowed": "1",
        "singleTransactionOnly": True,
        "linkOrderId": merchant_request_id[:50],
        "linkNotes": merchant_request_id,
        "redirectionUrlSuccess": _url_with_order(_return_base_url(), merchant_request_id),
        "redirectionUrlFailure": _url_with_order(_return_base_url(), merchant_request_id),
    }
    response = _paytm_request(_paytm_endpoints()["create_link"], request_body)
    response_body = response.get("body") or response
    short_url = str(response_body.get("shortUrl") or "").strip()
    link_id = str(response_body.get("linkId") or "").strip()
    returned_type = str(response_body.get("linkType") or "").strip().upper()
    returned_amount = _decimal_amount(response_body.get("amount"))
    expected_amount = _expected_amount()
    if not short_url or not link_id:
        raise HTTPException(status_code=502, detail="Paytm payment link was not returned")
    # Never send a generic/editable-amount Paytm link to the customer.
    # Paytm's FIXED link must echo FIXED and the exact plan amount.
    if returned_type != "FIXED" or returned_amount != expected_amount:
        raise HTTPException(
            status_code=502,
            detail=(
                "Paytm did not create a fixed ₹5,000 payment link. "
                "Please retry; no editable-amount link will be opened."
            ),
        )

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO subscriptions(
                user_id, plan, amount, status, payment_gateway,
                merchant_order_id, gateway_order_id, gateway_state,
                gateway_payload, checkout_url, updated_at
            ) VALUES (?, ?, ?, 'pending', 'paytm_link', ?, ?, 'ISSUED', ?, ?, ?)
            """,
            (
                user["id"],
                plan_id,
                float(plan["amount_rupees"]),
                merchant_request_id,
                link_id,
                _safe_json(response),
                short_url,
                _utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "gateway": "paytm_link",
        "merchant_order_id": merchant_request_id,
        "link_id": link_id,
        "checkout_url": short_url,
        "amount": plan["price"],
        "display_price": plan["display_price"],
        "currency": "INR",
        "plan": plan,
    }


@router.get("/status/{merchant_request_id}")
def paytm_status(merchant_request_id: str, authorization: str = Header(None)):
    user = get_current_user(authorization)
    return _sync_order(merchant_request_id, expected_user_id=user["id"])


async def _parse_callback_payload(request: Request):
    content_type = str(request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        data = await request.json()
        if isinstance(data, dict) and isinstance(data.get("body"), dict):
            payload = dict(data["body"])
            signature = (data.get("head") or {}).get("signature")
            if signature:
                payload["CHECKSUMHASH"] = signature
            return payload
        return dict(data or {})
    form = await request.form()
    return {str(key): str(value) for key, value in form.items()}


def _verify_callback(payload):
    checksum = str(payload.get("CHECKSUMHASH") or payload.get("checksumhash") or "")
    if not checksum:
        return False
    params = {
        str(key): str(value)
        for key, value in payload.items()
        if str(key).upper() != "CHECKSUMHASH"
    }
    credentials = _paytm_credentials()
    return bool(
        _checksum_module().verifySignature(params, credentials["merchant_key"], checksum)
    )


@router.post("/webhook")
async def paytm_webhook(request: Request):
    if not _paytm_configured():
        raise HTTPException(status_code=503, detail="Paytm is not configured")
    payload = await _parse_callback_payload(request)
    if not _verify_callback(payload):
        raise HTTPException(status_code=401, detail="Invalid Paytm callback checksum")

    credentials = _paytm_credentials()
    received_mid = str(payload.get("MID") or payload.get("mid") or "")
    if received_mid and received_mid != credentials["mid"]:
        raise HTTPException(status_code=401, detail="Invalid Paytm merchant ID")

    state = str(payload.get("STATUS") or payload.get("status") or "").upper()
    if state not in _SUCCESS_STATES:
        return {"status": "accepted", "payment_state": state or "UNKNOWN"}

    merchant_request_id = str(
        request.query_params.get("merchantRequestId")
        or payload.get("LINKNOTES")
        or payload.get("linkNotes")
        or ""
    ).strip()
    link_reference = str(
        payload.get("MERC_UNQ_REF") or payload.get("mercUniqRef") or ""
    ).strip()

    _shared_plan()
    conn = get_db()
    try:
        subscription_row = _find_subscription(
            conn,
            merchant_request_id=merchant_request_id,
            link_reference=link_reference,
        )
        if not subscription_row:
            raise HTTPException(status_code=404, detail="Paytm subscription order not found")
        activation = _activate_subscription(
            conn,
            subscription_row,
            payload,
            payload.get("TXNID") or payload.get("txnId"),
            payload.get("TXNAMOUNT") or payload.get("txnAmount"),
        )
        return {
            "status": "accepted",
            "subscription_active": True,
            "valid_till": activation["valid_till"],
        }
    finally:
        conn.close()


@router.post("/callback")
async def paytm_callback(request: Request):
    return await paytm_webhook(request)


@router.get("/return", response_class=HTMLResponse)
def paytm_return(merchantRequestId: str = ""):
    state = "PENDING"
    active = False
    message = "Payment verification is pending. Return to the app and tap Check Paytm Status."
    try:
        result = _sync_order(merchantRequestId)
        state = str(result.get("state") or "PENDING")
        active = bool(result.get("subscription_active"))
        if active:
            message = "Paytm payment verified. Your 30-day OKAI plan is active."
    except Exception:
        message = "Return to Option King AI and tap Check Paytm Status."

    colour = "#00a884" if active else "#e5a000"
    return HTMLResponse(
        "<!doctype html><html><head>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>OKAI Paytm Payment</title></head>"
        '<body style="font-family:Arial;background:#0a0a0f;color:#e8e8f0;padding:28px;text-align:center">'
        '<div style="max-width:520px;margin:40px auto;background:#13131f;border:1px solid #252540;border-radius:18px;padding:28px">'
        f'<div style="font-size:42px">{"✅" if active else "⏳"}</div>'
        f'<h2 style="color:{colour}">Paytm Payment {escape(state.title())}</h2>'
        f'<p style="line-height:1.6">{escape(message)}</p>'
        '<p style="color:#9090ad;font-size:13px">You may close this page and reopen Option King AI.</p>'
        "</div></body></html>"
    )
