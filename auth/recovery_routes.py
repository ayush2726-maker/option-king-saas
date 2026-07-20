import hashlib
import hmac
import os
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth.utils import SECRET_KEY, hash_password
from database import get_db


router = APIRouter(prefix="/auth", tags=["Account Recovery"])

RESET_EXPIRY_MINUTES = max(5, int(os.getenv("PASSWORD_RESET_EXPIRY_MINUTES", "10")))
RESET_MAX_ATTEMPTS = max(3, int(os.getenv("PASSWORD_RESET_MAX_ATTEMPTS", "5")))
RATE_LIMIT_WINDOW_MINUTES = max(5, int(os.getenv("AUTH_RECOVERY_RATE_WINDOW_MINUTES", "15")))


class RecoverLoginIdRequest(BaseModel):
    name: str
    phone: str


class RequestPasswordResetRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    otp: str
    new_password: str


def _utcnow():
    return datetime.utcnow()


def _normalise_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) < 11 or len(digits) > 15:
        raise HTTPException(status_code=400, detail="Enter a valid registered WhatsApp number")
    return "+" + digits


def _normalise_email(value):
    email = str(value or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        raise HTTPException(status_code=400, detail="Enter a valid registered email")
    return email


def _mask_email(email):
    local, domain = str(email).split("@", 1)
    if len(local) <= 2:
        masked_local = local[:1] + "*"
    else:
        masked_local = local[:2] + "*" * min(6, max(2, len(local) - 2))
    domain_parts = domain.split(".")
    host = domain_parts[0]
    masked_host = host[:1] + "*" * min(5, max(2, len(host) - 1))
    suffix = "." + ".".join(domain_parts[1:]) if len(domain_parts) > 1 else ""
    return f"{masked_local}@{masked_host}{suffix}"


def _scope_hash(value):
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        str(value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def ensure_recovery_schema():
    conn = get_db()
    try:
        try:
            conn.execute("ALTER TABLE users ADD COLUMN password_changed_at TEXT")
        except Exception:
            pass

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code_hash TEXT NOT NULL,
                nonce TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                used_at TEXT,
                delivery_channel TEXT NOT NULL DEFAULT 'email',
                delivery_status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL,
                request_ip_hash TEXT,
                user_agent TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_password_reset_user
            ON password_reset_codes(user_id, requested_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_recovery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                identifier_hash TEXT NOT NULL,
                ip_hash TEXT,
                successful INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_auth_recovery_rate
            ON auth_recovery_events(action, identifier_hash, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_auth_recovery_ip
            ON auth_recovery_events(action, ip_hash, created_at DESC)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _request_ip(request):
    forwarded = str(request.headers.get("x-forwarded-for", "")).split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _enforce_rate_limit(conn, action, identifier, request):
    now = _utcnow()
    since = (now - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)).isoformat()
    identifier_hash = _scope_hash(f"{action}:identifier:{identifier}")
    ip_hash = _scope_hash(f"{action}:ip:{_request_ip(request)}")

    identifier_count = conn.execute(
        """
        SELECT COUNT(*) AS c FROM auth_recovery_events
        WHERE action=? AND identifier_hash=? AND datetime(created_at) >= datetime(?)
        """,
        (action, identifier_hash, since),
    ).fetchone()["c"]
    ip_count = conn.execute(
        """
        SELECT COUNT(*) AS c FROM auth_recovery_events
        WHERE action=? AND ip_hash=? AND datetime(created_at) >= datetime(?)
        """,
        (action, ip_hash, since),
    ).fetchone()["c"]

    if int(identifier_count or 0) >= 5 or int(ip_count or 0) >= 20:
        raise HTTPException(
            status_code=429,
            detail="Too many recovery attempts. Please try again after 15 minutes.",
        )

    conn.execute(
        """
        INSERT INTO auth_recovery_events(action, identifier_hash, ip_hash, successful, created_at)
        VALUES (?, ?, ?, 0, ?)
        """,
        (action, identifier_hash, ip_hash, now.isoformat()),
    )
    conn.commit()
    return identifier_hash, ip_hash


def _mark_latest_event_success(conn, action, identifier_hash, ip_hash):
    row = conn.execute(
        """
        SELECT id FROM auth_recovery_events
        WHERE action=? AND identifier_hash=? AND ip_hash=?
        ORDER BY id DESC LIMIT 1
        """,
        (action, identifier_hash, ip_hash),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE auth_recovery_events SET successful=1 WHERE id=?",
            (row["id"],),
        )


def _smtp_configured():
    return bool(
        str(os.getenv("SMTP_HOST", "")).strip()
        and str(os.getenv("SMTP_FROM_EMAIL", os.getenv("SMTP_USERNAME", ""))).strip()
    )


def _send_reset_email(to_email, otp, expires_minutes):
    host = str(os.getenv("SMTP_HOST", "")).strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    username = str(os.getenv("SMTP_USERNAME", "")).strip()
    password = str(os.getenv("SMTP_PASSWORD", ""))
    from_email = str(os.getenv("SMTP_FROM_EMAIL", username)).strip()
    from_name = str(os.getenv("SMTP_FROM_NAME", "Option King AI")).strip()
    use_ssl = str(os.getenv("SMTP_USE_SSL", "0")).lower() in {"1", "true", "yes"} or port == 465
    use_tls = str(os.getenv("SMTP_USE_TLS", "1")).lower() not in {"0", "false", "no"}

    if not host or not from_email:
        raise RuntimeError("SMTP is not configured")

    message = EmailMessage()
    message["Subject"] = "Option King AI password reset OTP"
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = to_email
    message.set_content(
        "Your Option King AI password reset OTP is: "
        f"{otp}\n\nThis OTP is valid for {expires_minutes} minutes and can be used only once. "
        "Do not share it with anyone. If you did not request this reset, ignore this email."
    )

    context = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
            if username:
                server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            if use_tls:
                server.starttls(context=context)
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(message)


def _code_hash(user_id, nonce, otp):
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        f"{user_id}:{nonce}:{otp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@router.get("/recovery-status")
def recovery_status():
    ensure_recovery_schema()
    return {
        "success": True,
        "email_otp_available": _smtp_configured(),
        "otp_expiry_minutes": RESET_EXPIRY_MINUTES,
    }


@router.post("/recover-login-id")
def recover_login_id(body: RecoverLoginIdRequest, request: Request):
    ensure_recovery_schema()
    name = " ".join(str(body.name or "").strip().lower().split())
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Enter the registered full name")
    phone = _normalise_phone(body.phone)

    conn = get_db()
    try:
        identifier_hash, ip_hash = _enforce_rate_limit(
            conn,
            "recover_login_id",
            f"{name}|{phone}",
            request,
        )
        user = conn.execute(
            """
            SELECT id, email FROM users
            WHERE lower(trim(name))=? AND phone=? AND is_active=1
            LIMIT 1
            """,
            (name, phone),
        ).fetchone()
        if not user:
            return {
                "success": True,
                "found": False,
                "message": "Details did not match an active account.",
            }

        _mark_latest_event_success(conn, "recover_login_id", identifier_hash, ip_hash)
        conn.commit()
        return {
            "success": True,
            "found": True,
            "masked_email": _mask_email(user["email"]),
            "message": "Your login ID is the registered email shown below.",
        }
    finally:
        conn.close()


@router.post("/request-password-reset")
def request_password_reset(body: RequestPasswordResetRequest, request: Request):
    ensure_recovery_schema()
    email = _normalise_email(body.email)
    service_available = _smtp_configured()
    generic = {
        "success": True,
        "email_otp_available": service_available,
        "message": (
            "If this email is registered, a one-time password reset code has been sent."
            if service_available
            else "Password reset email service is not configured yet. Please contact support."
        ),
    }

    conn = get_db()
    try:
        identifier_hash, ip_hash = _enforce_rate_limit(
            conn,
            "request_password_reset",
            email,
            request,
        )
        user = conn.execute(
            "SELECT id, email, is_active FROM users WHERE email=? LIMIT 1",
            (email,),
        ).fetchone()
        if not user or not user["is_active"] or not service_available:
            return generic

        now = _utcnow()
        expires_at = now + timedelta(minutes=RESET_EXPIRY_MINUTES)
        otp = f"{secrets.randbelow(1000000):06d}"
        nonce = secrets.token_hex(16)
        hashed = _code_hash(user["id"], nonce, otp)

        conn.execute(
            """
            UPDATE password_reset_codes
            SET used_at=?
            WHERE user_id=? AND used_at IS NULL
            """,
            (now.isoformat(), user["id"]),
        )
        cursor = conn.execute(
            """
            INSERT INTO password_reset_codes(
                user_id, code_hash, nonce, expires_at, attempts, max_attempts,
                used_at, delivery_channel, delivery_status, requested_at,
                request_ip_hash, user_agent
            ) VALUES (?, ?, ?, ?, 0, ?, NULL, 'email', 'pending', ?, ?, ?)
            """,
            (
                user["id"],
                hashed,
                nonce,
                expires_at.isoformat(),
                RESET_MAX_ATTEMPTS,
                now.isoformat(),
                ip_hash,
                str(request.headers.get("user-agent", ""))[:500],
            ),
        )
        reset_id = cursor.lastrowid
        conn.commit()

        try:
            _send_reset_email(user["email"], otp, RESET_EXPIRY_MINUTES)
            conn.execute(
                "UPDATE password_reset_codes SET delivery_status='sent' WHERE id=?",
                (reset_id,),
            )
            _mark_latest_event_success(conn, "request_password_reset", identifier_hash, ip_hash)
            conn.commit()
        except Exception as exc:
            conn.execute(
                "UPDATE password_reset_codes SET delivery_status='failed', used_at=? WHERE id=?",
                (_utcnow().isoformat(), reset_id),
            )
            conn.commit()
            print(f"PASSWORD RESET EMAIL FAILED | {str(exc)[:180]}")

        return generic
    finally:
        conn.close()


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, request: Request):
    ensure_recovery_schema()
    email = _normalise_email(body.email)
    otp = re.sub(r"\D", "", str(body.otp or ""))
    new_password = str(body.new_password or "")

    if len(otp) != 6:
        raise HTTPException(status_code=400, detail="Enter the 6-digit OTP")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    conn = get_db()
    try:
        identifier_hash, ip_hash = _enforce_rate_limit(
            conn,
            "reset_password",
            email,
            request,
        )
        user = conn.execute(
            "SELECT id, is_active FROM users WHERE email=? LIMIT 1",
            (email,),
        ).fetchone()
        if not user or not user["is_active"]:
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")

        row = conn.execute(
            """
            SELECT * FROM password_reset_codes
            WHERE user_id=? AND used_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (user["id"],),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")

        now = _utcnow()
        if datetime.fromisoformat(row["expires_at"]) < now:
            conn.execute(
                "UPDATE password_reset_codes SET used_at=? WHERE id=?",
                (now.isoformat(), row["id"]),
            )
            conn.commit()
            raise HTTPException(status_code=400, detail="OTP expired. Request a new code.")

        if int(row["attempts"] or 0) >= int(row["max_attempts"] or RESET_MAX_ATTEMPTS):
            conn.execute(
                "UPDATE password_reset_codes SET used_at=? WHERE id=?",
                (now.isoformat(), row["id"]),
            )
            conn.commit()
            raise HTTPException(status_code=400, detail="Too many incorrect attempts. Request a new code.")

        supplied_hash = _code_hash(user["id"], row["nonce"], otp)
        if not hmac.compare_digest(supplied_hash, row["code_hash"]):
            attempts = int(row["attempts"] or 0) + 1
            conn.execute(
                "UPDATE password_reset_codes SET attempts=? WHERE id=?",
                (attempts, row["id"]),
            )
            conn.commit()
            remaining = max(0, int(row["max_attempts"] or RESET_MAX_ATTEMPTS) - attempts)
            raise HTTPException(
                status_code=400,
                detail=f"Incorrect OTP. {remaining} attempt(s) remaining.",
            )

        changed_at = now.isoformat()
        conn.execute(
            "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
            (hash_password(new_password), changed_at, user["id"]),
        )
        conn.execute(
            "UPDATE password_reset_codes SET used_at=? WHERE user_id=? AND used_at IS NULL",
            (changed_at, user["id"]),
        )
        _mark_latest_event_success(conn, "reset_password", identifier_hash, ip_hash)
        conn.commit()

        return {
            "success": True,
            "message": "Password reset successfully. Please login with the new password.",
        }
    finally:
        conn.close()
