from datetime import datetime, timedelta
import io
import os
from html import escape
from urllib.parse import quote, urlencode

import qrcode
import requests
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse, Response

from auth.routes import get_current_user
from database import get_db
from subscription.routes import PLAN, PLAN_ID, _ensure_subscription_schema

router = APIRouter(prefix="/subscription/razorpay", tags=["Subscription - Razorpay"])


def _creds():
    return str(os.getenv("RAZORPAY_KEY_ID", "")).strip(), str(os.getenv("RAZORPAY_KEY_SECRET", "")).strip()


def _configured():
    key, secret = _creds()
    return bool(key and secret)


def _manual_upi_id():
    return str(os.getenv("MANUAL_UPI_ID", "")).strip()


def _manual_upi_name():
    return str(os.getenv("MANUAL_UPI_NAME", "Option King AI")).strip() or "Option King AI"


def _manual_upi_uri():
    upi_id = _manual_upi_id()
    if not upi_id:
        return ""
    return "upi://pay?" + urlencode({
        "pa": upi_id,
        "pn": _manual_upi_name(),
        "am": "5000.00",
        "cu": "INR",
        "tn": "Option King AI 30 day subscription",
    })


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


@router.get("/manual")
def manual_payment_details(authorization: str = Header(None)):
    user = get_current_user(authorization)
    upi_id = _manual_upi_id()
    configured = bool(upi_id)
    return {
        "success": True,
        "configured": configured,
        "mode": "manual",
        "automatic_activation": False,
        "amount_rupees": 5000,
        "display_amount": "₹5,000",
        "duration_days": 30,
        "upi_id": upi_id if configured else "",
        "upi_name": _manual_upi_name(),
        "upi_uri": _manual_upi_uri() if configured else "",
        "qr_url": "https://option-king-saas-production.up.railway.app/subscription/razorpay/manual/qr" if configured else "",
        "user_reference": str(user["email"] or user["id"]),
        "instructions": "Pay ₹5,000 using this UPI or QR. After payment, admin confirmation is required. Account activation is manual for 30 days.",
    }


@router.get("/manual/qr")
def manual_payment_qr():
    uri = _manual_upi_uri()
    if not uri:
        raise HTTPException(status_code=503, detail="Manual UPI ID is not configured")
    image = qrcode.make(uri)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Response(content=output.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/manual-page", response_class=HTMLResponse)
def manual_payment_page(ref: str = ""):
    upi_id = _manual_upi_id()
    upi_name = _manual_upi_name()
    upi_uri = _manual_upi_uri()
    configured = bool(upi_id)
    safe_ref = escape(str(ref or ""))
    if configured:
        pay_button = f"<a href='{escape(upi_uri)}' style='display:block;background:#00d4a0;color:#06111a;text-decoration:none;font-weight:800;padding:14px 18px;border-radius:12px;margin-top:16px'>Open UPI App</a>"
        qr = "<img src='/subscription/razorpay/manual/qr' alt='UPI QR' style='width:230px;height:230px;background:white;padding:10px;border-radius:16px;margin:16px auto;display:block'/>"
        upi_html = f"<div style='font-size:14px;color:#aebbd0'>UPI ID</div><div style='font-size:20px;font-weight:800;word-break:break-all'>{escape(upi_id)}</div>"
    else:
        pay_button = ""
        qr = ""
        upi_html = "<div style='color:#ff6b7d;font-weight:700'>Payment UPI is not configured yet.</div>"
    ref_html = f"<div style='margin-top:10px;color:#7f8da3;font-size:12px'>Reference: {safe_ref}</div>" if safe_ref else ""
    return HTMLResponse(f"""
<!doctype html>
<html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>Option King AI Payment</title></head>
<body style='margin:0;background:#070b12;color:#eef4ff;font-family:Arial,sans-serif;padding:20px'>
  <div style='max-width:420px;margin:24px auto;background:#111827;border:1px solid #273449;border-radius:20px;padding:22px;box-shadow:0 16px 40px rgba(0,0,0,.35)'>
    <div style='font-size:13px;color:#f5c842;font-weight:800'>OPTION KING AI</div>
    <h2 style='margin:8px 0 4px'>30-Day Subscription</h2>
    <div style='font-size:32px;font-weight:900;color:#00d4a0'>₹5,000</div>
    <div style='color:#9aa8bd;margin-top:4px'>Manual UPI / QR payment</div>
    {qr}
    {upi_html}
    {pay_button}
    {ref_html}
    <div style='margin-top:20px;padding:14px;border-radius:12px;background:#0b1220;border:1px solid #243248;color:#c5d1e2;font-size:13px;line-height:1.55'>
      Payment karne ke baad account automatically activate nahi hoga. Admin payment confirm karke account ko 30 days ke liye manually activate karega.
    </div>
  </div>
</body></html>
""")


@router.get("/config")
def config(authorization: str = Header(None)):
    get_current_user(authorization)
    return {"success": True, "available": _configured(), "plan": PLAN, "upi_supported": True, "qr_supported": True}


@router.post("/create-link")
def create_link(body: dict = None, authorization: str = Header(None)):
    user = get_current_user(authorization)
    if not _manual_upi_id():
        raise HTTPException(status_code=503, detail="Manual UPI ID is not configured yet")
    reference = str(user["email"] or user["id"])
    checkout_url = "https://option-king-saas-production.up.railway.app/subscription/razorpay/manual-page?ref=" + quote(reference, safe="")
    return {
        "success": True,
        "manual_payment": True,
        "automatic_activation": False,
        "checkout_url": checkout_url,
        "amount_rupees": 5000,
        "upi_supported": True,
        "qr_supported": True,
        "message": "Manual UPI/QR payment page created. Admin activation required after payment.",
    }


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
    return HTMLResponse("<html><body style='font-family:Arial;background:#07101b;color:#fff;padding:40px'><h2>Option King AI</h2><p>Automatic payment activation is disabled. Please use the manual UPI/QR payment page and contact admin after payment.</p></body></html>")
