from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, EmailStr
from datetime import datetime, timedelta
from database import get_db
from auth.utils import hash_password, verify_password, create_access_token, decode_token

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ─── Request Models ───────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    phone: str = None


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

@router.post("/register")
def register(req: RegisterRequest):
    # Validate
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

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
        (req.name.strip(), req.email.lower().strip(), password_hash, req.phone, trial_ends)
    )
    user_id = cursor.lastrowid

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
            "trial_ends_at": trial_ends
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
