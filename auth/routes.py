from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel, EmailStr
from datetime import datetime, timedelta
import hashlib
import json
import re
from database import get_db
from auth.utils import hash_password, verify_password, create_access_token, decode_token

router = APIRouter(prefix="/auth", tags=["Authentication"])


REGISTRATION_POLICY_VERSION = "OKAI-RISK-2026-07-20-v1"

REGISTRATION_DISCLOSURE = {
    "policy_version": REGISTRATION_POLICY_VERSION,
    "title": "Option King AI - Risk, Terms and Automated Trading Acknowledgement",
    "summary": (
        "Registration is completed only after the user separately accepts "
        "the disclosures below."
    ),
    "acknowledgements": [
        {
            "key": "age_confirmed",
            "required": True,
            "text": "I confirm that I am at least 18 years old and legally capable of entering into this agreement.",
        },
        {
            "key": "risk_acknowledged",
            "required": True,
            "text": (
                "I understand that options and derivatives trading involves substantial risk, "
                "rapid losses and possible loss of the entire trading capital."
            ),
        },
        {
            "key": "no_guarantee",
            "required": True,
            "text": (
                "I understand that Option King AI does not promise or guarantee profit, returns, "
                "accuracy, loss recovery or risk-free trading. Backtests and past performance do not "
                "guarantee future results."
            ),
        },
        {
            "key": "algo_order_authorized",
            "required": True,
            "text": (
                "I request automated order functionality only after all broker, exchange and legal "
                "approvals/consents required for my account are in place. I remain responsible for my "
                "broker account, capital, settings, live-mode activation and risk limits."
            ),
        },
        {
            "key": "technology_risk",
            "required": True,
            "text": (
                "I understand that market-data, internet, mobile, server, broker API, order-routing, "
                "software or exchange failures may cause delayed, duplicate, rejected, missed or "
                "incorrect orders and losses."
            ),
        },
        {
            "key": "terms_accepted",
            "required": True,
            "text": (
                "I have read and accept the Terms of Use and Risk Disclosure. The subscription fee is "
                "for access to software features and is not a fee for guaranteed returns."
            ),
        },
        {
            "key": "privacy_accepted",
            "required": True,
            "text": (
                "I have read and accept the Privacy Notice and consent to processing of the information "
                "needed to provide, secure and audit the service."
            ),
        },
        {
            "key": "whatsapp_trade_alert_opt_in",
            "required": True,
            "text": (
                "I agree to receive only my executed PAPER/LIVE trade alerts and essential account "
                "messages on the WhatsApp number provided by me."
            ),
        },
    ],
    "important_note": (
        "This acknowledgement does not remove any statutory rights of the user and does not replace "
        "SEBI, exchange, broker, algo-provider or other legal compliance requirements."
    ),
}


def _registration_policy_text():
    return json.dumps(
        REGISTRATION_DISCLOSURE,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _registration_policy_hash():
    return hashlib.sha256(
        _registration_policy_text().encode("utf-8")
    ).hexdigest()


def _normalise_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) < 11 or len(digits) > 15:
        raise HTTPException(
            status_code=400,
            detail="Valid WhatsApp number with country code is required",
        )
    return "+" + digits


# ─── Request Models ───────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    phone: str
    policy_version: str
    age_confirmed: bool
    risk_acknowledged: bool
    no_guarantee_acknowledged: bool = None
    technology_risk_acknowledged: bool = None
    terms_accepted: bool
    privacy_accepted: bool
    algo_order_authorized: bool
    whatsapp_trade_alert_opt_in: bool


class LoginRequest(BaseModel):
    email: str
    password: str


# ─── Helper ───────────────────────────────────────────────────────

def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Login required")
    token = authorization.split(" ")[1]
    try:
        payload = decode_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?", (payload["user_id"],)
    ).fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account suspended")

    return dict(user)


# ─── Routes ───────────────────────────────────────────────────────

@router.get("/registration-disclosure")
def registration_disclosure():
    return {
        "success": True,
        "disclosure": REGISTRATION_DISCLOSURE,
        "policy_hash": _registration_policy_hash(),
    }


@router.post("/register")
def register(req: RegisterRequest, request: Request):
    # Validate
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    if req.policy_version != REGISTRATION_POLICY_VERSION:
        raise HTTPException(
            status_code=409,
            detail=(
                "Risk disclosure has changed. Please reload registration "
                "and review the latest rules."
            ),
        )

    required_acceptances = {
        "age confirmation": req.age_confirmed,
        "trading-risk acknowledgement": req.risk_acknowledged,
        "no-profit-guarantee acknowledgement": (req.no_guarantee_acknowledged if req.no_guarantee_acknowledged is not None else req.risk_acknowledged),
        "technology and broker-API risk acknowledgement": (req.technology_risk_acknowledged if req.technology_risk_acknowledged is not None else req.risk_acknowledged),
        "Terms of Use": req.terms_accepted,
        "Privacy Notice": req.privacy_accepted,
        "automated-order acknowledgement": req.algo_order_authorized,
        "WhatsApp trade-alert consent": req.whatsapp_trade_alert_opt_in,
    }
    missing = [
        label
        for label, accepted in required_acceptances.items()
        if not accepted
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "Registration acknowledgement is required: "
                + ", ".join(missing)
            ),
        )

    normalised_phone = _normalise_phone(req.phone)
    conn = get_db()

    # Check email exists
    existing = conn.execute(
        "SELECT id FROM users WHERE email = ?", (req.email.lower().strip(),)
    ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail="Email already registered")

    # Create user with 7-day trial
    trial_ends = (datetime.utcnow() + timedelta(days=7)).isoformat()
    password_hash = hash_password(req.password)

    cursor = conn.execute(
        """INSERT INTO users (name, email, password_hash, phone, trial_ends_at, subscription_status)
           VALUES (?, ?, ?, ?, ?, 'trial')""",
        (req.name.strip(), req.email.lower().strip(), password_hash, normalised_phone, trial_ends)
    )
    user_id = cursor.lastrowid

    accepted_at = datetime.utcnow().isoformat()
    policy_text = _registration_policy_text()
    policy_hash = _registration_policy_hash()
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")[:500]

    conn.execute(
        """
        INSERT INTO user_consents (
            user_id,
            policy_version,
            policy_hash,
            accepted_text,
            age_confirmed,
            risk_acknowledged,
            no_guarantee_acknowledged,
            technology_risk_acknowledged,
            terms_accepted,
            privacy_accepted,
            algo_order_authorized,
            whatsapp_trade_alert_opt_in,
            accepted_at,
            ip_address,
            user_agent
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            REGISTRATION_POLICY_VERSION,
            policy_hash,
            policy_text,
            int(req.age_confirmed),
            int(req.risk_acknowledged),
            int(req.no_guarantee_acknowledged if req.no_guarantee_acknowledged is not None else req.risk_acknowledged),
            int(req.technology_risk_acknowledged if req.technology_risk_acknowledged is not None else req.risk_acknowledged),
            int(req.terms_accepted),
            int(req.privacy_accepted),
            int(req.algo_order_authorized),
            int(req.whatsapp_trade_alert_opt_in),
            accepted_at,
            ip_address,
            user_agent,
        ),
    )

    # Create bot_status entry
    conn.execute(
        "INSERT INTO bot_status (user_id) VALUES (?)", (user_id,)
    )
    conn.commit()

    # Generate token
    token = create_access_token(user_id, req.email)

    conn.close()

    return {
        "success": True,
        "message": f"Welcome {req.name}! Your 7-day free trial has started 🎉",
        "token": token,
        "user": {
            "id": user_id,
            "name": req.name,
            "email": req.email.lower(),
            "subscription_status": "trial",
            "trial_ends_at": trial_ends,
            "phone": normalised_phone,
            "consent_policy_version": REGISTRATION_POLICY_VERSION,
            "consent_policy_hash": policy_hash,
            "consent_accepted_at": accepted_at
        }
    }


@router.post("/login")
def login(req: LoginRequest):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ?", (req.email.lower().strip(),)
    ).fetchone()
    conn.close()

    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")

    # Check subscription status
    status = user["subscription_status"]
    trial_ends = user["trial_ends_at"]
    warning = None

    if status == "trial" and trial_ends:
        trial_end_dt = datetime.fromisoformat(trial_ends)
        days_left = (trial_end_dt - datetime.utcnow()).days
        if days_left <= 0:
            # Update to expired
            conn = get_db()
            conn.execute(
                "UPDATE users SET subscription_status='expired' WHERE id=?", (user["id"],)
            )
            conn.commit()
            conn.close()
            status = "expired"
            warning = "Your trial has expired. Please subscribe to continue."
        elif days_left <= 2:
            warning = f"Trial expires in {days_left} day(s). Subscribe now!"

    token = create_access_token(user["id"], user["email"])

    return {
        "success": True,
        "token": token,
        "warning": warning,
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "phone": user["phone"],
            "subscription_status": status,
            "trial_ends_at": trial_ends,
            "is_admin": bool(user["is_admin"])
        }
    }


@router.get("/me")
def get_me(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()

    # Get broker info
    brokers = conn.execute(
        "SELECT broker_name, is_active, last_connected FROM broker_credentials WHERE user_id = ?",
        (user["id"],)
    ).fetchall()

    # Get bot status
    bot = conn.execute(
        "SELECT * FROM bot_status WHERE user_id = ?", (user["id"],)
    ).fetchone()

    # Get active subscription
    sub = conn.execute(
        """SELECT * FROM subscriptions WHERE user_id = ? AND status='active'
           ORDER BY created_at DESC LIMIT 1""",
        (user["id"],)
    ).fetchone()

    conn.close()

    return {
        "success": True,
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "phone": user["phone"],
            "subscription_status": user["subscription_status"],
            "trial_ends_at": user["trial_ends_at"],
            "is_admin": bool(user["is_admin"]),
            "brokers_connected": [dict(b) for b in brokers],
            "bot": dict(bot) if bot else None,
            "active_subscription": dict(sub) if sub else None
        }
    }


@router.post("/change-password")
def change_password(
    body: dict,
    authorization: str = Header(None)
):
    user = get_current_user(authorization)

    old_password = body.get("old_password")
    new_password = body.get("new_password")

    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="old_password and new_password required")

    if not verify_password(old_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (hash_password(new_password), user["id"])
    )
    conn.commit()
    conn.close()

    return {"success": True, "message": "Password changed successfully"}
