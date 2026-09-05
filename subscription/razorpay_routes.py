from datetime import datetime, timedelta
import os

import requests
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse

from auth.routes import get_current_user
from database import get_db
from subscription.routes import PLAN, PLAN_ID, _ensure_subscription_schema

router = APIRouter(prefix="/subscription/razorpay", tags=["Subscription - Razorpay"])


def _creds():
    return str(os.getenv("RAZORPAY_KEY_ID", "")).strip(), str(os.getenv("RAZORPAY_KEY_SECRET", "")).strip()


def _configured():
    key, secret = _creds()
    return bool(key and secret)


def _api(method, path, **kwargs):
    key, secret = _creds()
    if not key or not secret:
        raise HTTPException(status_code=503, detail="Razorpay merchant credentials are not configured")
    r = requests.request(method, "https://api.razorpay.com/v1" + path, auth=(key, secret), timeout=25, **kwargs)
    try:
        data = r.json()
    except Exception:
        data = {}
    if not r.ok:
        detail = ((data.get("error") or {}).get("description") if isinstance(data, dict) else None) or r.text
        raise HTTPException(status_code=502, detail=f"Razorpay error: {str(detail)[:180]}")
    return data


def _activate_if_paid(user_id, payment_link_id, payload):
    status = str(payload.get("status") or "").lower()
    amount_paid = int(payload.get("amount_paid") or 0)
    if status != "paid" or amount_paid != int(PLAN["price"]):
        return {"active": False, "status": status or "pending"}

    _ensure_subscription_schema()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND gateway_order_id=? ORDER BY id DESC LIMIT 1",
            (int(user_id), str(payment_link_id)),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Subscription payment record not found")
        if str(row["status"] or "").lower() == "active":
            return {"active": True, "valid_till": row["valid_till"]}

        now = datetime.utcnow()
        base = now
        current = conn.execute(
            "SELECT valid_till FROM subscriptions WHERE user_id=? AND status='active' AND valid_till IS NOT NULL ORDER BY datetime(valid_till) DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
        if current and current["valid_till"]:
            try:
                dt = datetime.fromisoformat(current["valid_till"])
                if dt > now:
                    base = dt
            except Exception:
                pass
        valid_till = base + timedelta(days=int(PLAN["duration_days"]))
        stamp = now.isoformat()
        payment_id = ""
        payments = payload.get("payments") or []
        if payments and isinstance(payments, list):
            payment_id = str((payments[-1] or {}).get("payment_id") or (payments[-1] or {}).get("id") or "")
        conn.execute(
            """
            UPDATE subscriptions SET status='active', gateway_state='PAID',
              gateway_transaction_id=?, gateway_payload=?, valid_from=?, valid_till=?, activated_at=?, updated_at=?
            WHERE id=?
            """,
            (payment_id, str(payload)[:12000], stamp, valid_till.isoformat(), stamp, stamp, row["id"]),
        )
        conn.execute(
            "UPDATE users SET is_active=1, subscription_status='active', trial_ends_at=NULL WHERE id=?",
            (int(user_id),),
        )
        conn.commit()
        return {"active": True, "valid_till": valid_till.isoformat()}
    finally:
        conn.close()


@router.get("/config")
def config(authorization: str = Header(None)):
    get_current_user(authorization)
    return {"success": True, "available": _configured(), "plan": PLAN, "upi_supported": True, "qr_supported": True}


@router.post("/create-link")
def create_link(body: dict = None, authorization: str = Header(None)):
    user = get_current_user(authorization)
    _ensure_subscription_schema()
    callback_base = str(os.getenv("RAZORPAY_CALLBACK_URL", "https://option-king-saas-production.up.railway.app/subscription/razorpay/return")).strip()
    payload = {
        "amount": int(PLAN["price"]),
        "currency": "INR",
        "accept_partial": False,
        "description": "Option King AI - 30 Day Subscription",
        "reference_id": f"OKAI-{int(user['id'])}-{int(datetime.utcnow().timestamp())}",
        "customer": {
            "name": str(user["name"] or "Option King User")[:64],
            "email": str(user["email"] or "")[:100],
        },
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"user_id": str(user["id"]), "plan": PLAN_ID},
        "callback_url": callback_base,
        "callback_method": "get",
    }
    phone = str(user["phone"] if "phone" in user.keys() else "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) >= 10:
        payload["customer"]["contact"] = digits[-10:]
    data = _api("POST", "/payment_links", json=payload)
    link_id = str(data.get("id") or "")
    short_url = str(data.get("short_url") or "")
    if not link_id or not short_url:
        raise HTTPException(status_code=502, detail="Payment link was not created")
    now = datetime.utcnow().isoformat()
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO subscriptions(user_id, plan, amount, status, payment_gateway,
              merchant_order_id, gateway_order_id, gateway_state, gateway_payload, checkout_url, updated_at)
            VALUES (?, ?, ?, 'pending', 'razorpay_payment_link', ?, ?, 'created', ?, ?, ?)
            """,
            (int(user["id"]), PLAN_ID, int(PLAN["amount_rupees"]), payload["reference_id"], link_id, str(data)[:12000], short_url, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "checkout_url": short_url, "payment_link_id": link_id, "amount_rupees": PLAN["amount_rupees"], "upi_supported": True, "qr_supported": True}


@router.get("/status/{payment_link_id}")
def status(payment_link_id: str, authorization: str = Header(None)):
    user = get_current_user(authorization)
    if not payment_link_id.startswith("plink_"):
        raise HTTPException(status_code=400, detail="Invalid payment link ID")
    data = _api("GET", f"/payment_links/{payment_link_id}")
    activation = _activate_if_paid(user["id"], payment_link_id, data)
    return {"success": True, "subscription_active": bool(activation.get("active")), "state": str(data.get("status") or "pending").upper(), "valid_till": activation.get("valid_till")}


@router.get("/return", response_class=HTMLResponse)
def payment_return(razorpay_payment_link_id: str = "", razorpay_payment_link_status: str = ""):
    link_id = str(razorpay_payment_link_id or "")
    message = "Payment received. Return to Option King AI and tap Refresh Activation Status."
    if link_id.startswith("plink_"):
        try:
            data = _api("GET", f"/payment_links/{link_id}")
            notes = data.get("notes") or {}
            user_id = int(notes.get("user_id") or 0)
            if user_id:
                result = _activate_if_paid(user_id, link_id, data)
                if result.get("active"):
                    message = "Payment verified successfully. Your Option King AI account is active for 30 days."
        except Exception:
            pass
    return HTMLResponse(f"<html><body style='font-family:Arial;background:#07101b;color:#fff;padding:40px'><h2>Option King AI</h2><p>{message}</p><p>You may close this page.</p></body></html>")
