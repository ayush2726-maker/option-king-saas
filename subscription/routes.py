from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel
from datetime import datetime, timedelta
import razorpay
import hmac
import hashlib
import os
from database import get_db
from auth.routes import get_current_user

router = APIRouter(prefix="/subscription", tags=["Subscription"])

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_XXXXXXXXXXXXXXXX")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "your_razorpay_secret")

PLANS = {
    "basic": {
        "name": "Basic Plan",
        "price": 99900,          # ₹999 in paise
        "display_price": "₹999",
        "duration_days": 30,
        "features": [
            "1 Broker Connection",
            "Live F&O Trading",
            "Real-time Signals",
            "Telegram Notifications",
            "Trade History"
        ]
    },
    "pro": {
        "name": "Pro Plan",
        "price": 199900,         # ₹1999 in paise
        "display_price": "₹1,999",
        "duration_days": 30,
        "features": [
            "3 Broker Connections",
            "Live F&O Trading",
            "Priority Signals",
            "Telegram + Push Notifications",
            "Advanced Analytics",
            "Trade History + Reports",
            "Priority Support"
        ]
    }
}


@router.get("/plans")
def get_plans():
    return {
        "success": True,
        "plans": PLANS,
        "trial": {
            "duration_days": 7,
            "description": "Full access for 7 days — no credit card required"
        }
    }


@router.post("/create-order")
def create_order(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)

    plan_id = body.get("plan")
    if plan_id not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan. Choose 'basic' or 'pro'")

    plan = PLANS[plan_id]

    try:
        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        order = client.order.create({
            "amount": plan["price"],
            "currency": "INR",
            "receipt": f"order_user_{user['id']}_{plan_id}_{int(datetime.utcnow().timestamp())}",
            "notes": {
                "user_id": str(user["id"]),
                "user_email": user["email"],
                "plan": plan_id
            }
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Payment gateway error: {str(e)}")

    # Save pending order
    conn = get_db()
    conn.execute(
        """INSERT INTO subscriptions (user_id, plan, amount, razorpay_order_id, status)
           VALUES (?, ?, ?, ?, 'pending')""",
        (user["id"], plan_id, plan["price"] / 100, order["id"])
    )
    conn.commit()
    conn.close()

    return {
        "success": True,
        "order_id": order["id"],
        "amount": plan["price"],
        "currency": "INR",
        "key": RAZORPAY_KEY_ID,
        "plan": plan_id,
        "plan_name": plan["name"],
        "user_name": user["name"],
        "user_email": user["email"]
    }


@router.post("/verify-payment")
def verify_payment(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)

    order_id = body.get("razorpay_order_id")
    payment_id = body.get("razorpay_payment_id")
    signature = body.get("razorpay_signature")

    if not all([order_id, payment_id, signature]):
        raise HTTPException(status_code=400, detail="Missing payment details")

    # Verify signature
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=400, detail="Payment verification failed — invalid signature")

    conn = get_db()

    # Get pending subscription
    sub = conn.execute(
        "SELECT * FROM subscriptions WHERE razorpay_order_id=? AND user_id=?",
        (order_id, user["id"])
    ).fetchone()

    if not sub:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found")

    # Calculate validity
    plan = PLANS.get(sub["plan"], PLANS["basic"])
    valid_from = datetime.utcnow()
    valid_till = valid_from + timedelta(days=plan["duration_days"])

    # Update subscription
    conn.execute(
        """UPDATE subscriptions
           SET status='active', razorpay_payment_id=?, valid_from=?, valid_till=?
           WHERE id=?""",
        (payment_id, valid_from.isoformat(), valid_till.isoformat(), sub["id"])
    )

    # Update user status
    conn.execute(
        "UPDATE users SET subscription_status='active' WHERE id=?",
        (user["id"],)
    )

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": f"🎉 {plan['name']} activated successfully!",
        "valid_till": valid_till.isoformat(),
        "plan": sub["plan"]
    }


@router.get("/status")
def get_subscription_status(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()

    # Get active subscription
    active_sub = conn.execute(
        """SELECT * FROM subscriptions WHERE user_id=? AND status='active'
           ORDER BY created_at DESC LIMIT 1""",
        (user["id"],)
    ).fetchone()

    # Get all subscriptions history
    history = conn.execute(
        "SELECT plan, amount, status, valid_from, valid_till, created_at FROM subscriptions WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],)
    ).fetchall()

    conn.close()

    # Days remaining
    days_remaining = None
    if active_sub and active_sub["valid_till"]:
        valid_till = datetime.fromisoformat(active_sub["valid_till"])
        days_remaining = max(0, (valid_till - datetime.utcnow()).days)
    elif user["subscription_status"] == "trial" and user["trial_ends_at"]:
        trial_end = datetime.fromisoformat(user["trial_ends_at"])
        days_remaining = max(0, (trial_end - datetime.utcnow()).days)

    return {
        "success": True,
        "subscription_status": user["subscription_status"],
        "days_remaining": days_remaining,
        "active_subscription": dict(active_sub) if active_sub else None,
        "history": [dict(h) for h in history]
    }


@router.post("/webhook")
async def razorpay_webhook(request: Request):
    """Razorpay webhook for payment events"""
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    data = await request.json()
    event = data.get("event")

    if event == "payment.captured":
        payment = data["payload"]["payment"]["entity"]
        order_id = payment.get("order_id")

        conn = get_db()
        sub = conn.execute(
            "SELECT * FROM subscriptions WHERE razorpay_order_id=?", (order_id,)
        ).fetchone()

        if sub and sub["status"] == "pending":
            plan = PLANS.get(sub["plan"], PLANS["basic"])
            valid_from = datetime.utcnow()
            valid_till = valid_from + timedelta(days=plan["duration_days"])

            conn.execute(
                """UPDATE subscriptions SET status='active', valid_from=?, valid_till=?
                   WHERE id=?""",
                (valid_from.isoformat(), valid_till.isoformat(), sub["id"])
            )
            conn.execute(
                "UPDATE users SET subscription_status='active' WHERE id=?",
                (sub["user_id"],)
            )
            conn.commit()

        conn.close()

    return {"status": "ok"}
