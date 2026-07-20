import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from auth.utils import SECRET_KEY, hash_password
from database import get_db


router = APIRouter(prefix="/auth", tags=["Account Recovery"])

OTP_EXPIRY_MINUTES = max(5, int(os.getenv("AUTH_OTP_EXPIRY_MINUTES", "10")))
OTP_MAX_ATTEMPTS = max(3, int(os.getenv("AUTH_OTP_MAX_ATTEMPTS", "5")))
VERIFICATION_TOKEN_MINUTES = max(10, int(os.getenv("EMAIL_VERIFICATION_TOKEN_MINUTES", "30")))
RATE_WINDOW_MINUTES = max(5, int(os.getenv("AUTH_RECOVERY_RATE_WINDOW_MINUTES", "15")))


class EmailRequest(BaseModel):
    email: str


class VerifyEmailRequest(BaseModel):
    email: str
    otp: str


class RecoverLoginIdRequest(BaseModel):
    name: str
    phone: str


class ResetPasswordRequest(BaseModel):
    email: str
    otp: str
    new_password: str


def _utcnow():
    return datetime.utcnow()


def _normalise_email(value):
    email = str(value or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    return email


def _normalise_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) < 11 or len(digits) > 15:
        raise HTTPException(status_code=400, detail="Enter a valid registered mobile number")
    return "+" + digits


def _mask_email(email):
    local, domain = str(email).split("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    masked_local = visible + "*" * max(2, min(6, len(local) - len(visible)))
    host, dot, suffix = domain.partition(".")
    masked_host = host[:1] + "*" * max(2, min(5, len(host) - 1))
    return f"{masked_local}@{masked_host}{dot}{suffix}"


def _secret_hash(scope, value):
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        f"{scope}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _otp_hash(purpose, identifier, nonce, otp):
    return _secret_hash(purpose, f"{identifier}:{nonce}:{otp}")


def _token_hash(email, token):
    return _secret_hash("registration-email-token", f"{email}:{token}")


def _request_ip(request):
    forwarded = str(request.headers.get("x-forwarded-for", "")).split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def ensure_recovery_schema():
    conn = get_db()
    try:
        try:
            conn.execute("ALTER TABLE users ADD COLUMN password_changed_at TEXT")
        except Exception:
            pass

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_otp_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purpose TEXT NOT NULL,
                identifier TEXT NOT NULL,
                user_id INTEGER,
                code_hash TEXT NOT NULL,
                nonce TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                used_at TEXT,
                verified_at TEXT,
                verification_token_hash TEXT,
                token_expires_at TEXT,
                consumed_at TEXT,
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
            CREATE INDEX IF NOT EXISTS idx_email_otp_lookup
            ON email_otp_codes(purpose, identifier, requested_at DESC)
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


def _rate_limit(conn, action, identifier, request):
    now = _utcnow()
    since = (now - timedelta(minutes=RATE_WINDOW_MINUTES)).isoformat()
    identifier_hash = _secret_hash(f"rate:{action}:identifier", identifier)
    ip_hash = _secret_hash(f"rate:{action}:ip", _request_ip(request))

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
            detail=f"Too many attempts. Try again after {RATE_WINDOW_MINUTES} minutes.",
        )

    cursor = conn.execute(
        """
        INSERT INTO auth_recovery_events(action, identifier_hash, ip_hash, successful, created_at)
        VALUES (?, ?, ?, 0, ?)
        """,
        (action, identifier_hash, ip_hash, now.isoformat()),
    )
    conn.commit()
    return cursor.lastrowid, ip_hash


def _mark_event_success(conn, event_id):
    conn.execute(
        "UPDATE auth_recovery_events SET successful=1 WHERE id=?",
        (event_id,),
    )


def _email_service_available():
    resend_ready = bool(os.getenv("RESEND_API_KEY") and os.getenv("EMAIL_FROM"))
    smtp_ready = bool(
        os.getenv("SMTP_HOST")
        and (os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_USERNAME"))
    )
    return resend_ready or smtp_ready


def _send_email(to_email, subject, body):
    resend_key = str(os.getenv("RESEND_API_KEY", "")).strip()
    email_from = str(os.getenv("EMAIL_FROM", "")).strip()
    if resend_key and email_from:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": email_from,
                "to": [to_email],
                "subject": subject,
                "text": body,
            },
            timeout=20,
        )
        response.raise_for_status()
        return

    host = str(os.getenv("SMTP_HOST", "")).strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    username = str(os.getenv("SMTP_USERNAME", "")).strip()
    password = str(os.getenv("SMTP_PASSWORD", ""))
    from_email = str(os.getenv("SMTP_FROM_EMAIL", username)).strip()
    from_name = str(os.getenv("SMTP_FROM_NAME", "Option King AI")).strip()
    use_ssl = str(os.getenv("SMTP_USE_SSL", "0")).lower() in {"1", "true", "yes"} or port == 465
    use_tls = str(os.getenv("SMTP_USE_TLS", "1")).lower() not in {"0", "false", "no"}

    if not host or not from_email:
        raise RuntimeError("Email service is not configured")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = to_email
    message.set_content(body)

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


def _create_and_send_otp(conn, purpose, email, user_id, request, subject, intro):
    now = _utcnow()
    expires_at = now + timedelta(minutes=OTP_EXPIRY_MINUTES)
    otp = f"{secrets.randbelow(1000000):06d}"
    nonce = secrets.token_hex(16)
    code_hash = _otp_hash(purpose, email, nonce, otp)
    ip_hash = _secret_hash(f"otp:{purpose}:ip", _request_ip(request))

    conn.execute(
        """
        UPDATE email_otp_codes SET used_at=?
        WHERE purpose=? AND identifier=? AND used_at IS NULL
        """,
        (now.isoformat(), purpose, email),
    )
    cursor = conn.execute(
        """
        INSERT INTO email_otp_codes(
            purpose, identifier, user_id, code_hash, nonce, expires_at,
            attempts, max_attempts, used_at, verified_at,
            verification_token_hash, token_expires_at, consumed_at,
            delivery_status, requested_at, request_ip_hash, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL, NULL, NULL,
                  'pending', ?, ?, ?)
        """,
        (
            purpose,
            email,
            user_id,
            code_hash,
            nonce,
            expires_at.isoformat(),
            OTP_MAX_ATTEMPTS,
            now.isoformat(),
            ip_hash,
            str(request.headers.get("user-agent", ""))[:500],
        ),
    )
    otp_id = cursor.lastrowid
    conn.commit()

    try:
        _send_email(
            email,
            subject,
            f"{intro}\n\nOTP: {otp}\n\nThis OTP is valid for {OTP_EXPIRY_MINUTES} minutes and can be used only once. Do not share it with anyone.",
        )
        conn.execute(
            "UPDATE email_otp_codes SET delivery_status='sent' WHERE id=?",
            (otp_id,),
        )
        conn.commit()
    except Exception as exc:
        conn.execute(
            "UPDATE email_otp_codes SET delivery_status='failed', used_at=? WHERE id=?",
            (_utcnow().isoformat(), otp_id),
        )
        conn.commit()
        print(f"EMAIL OTP SEND FAILED | {purpose} | {str(exc)[:180]}")
        raise HTTPException(status_code=503, detail="Could not send email OTP. Please try again.")


def _get_valid_code(conn, purpose, email):
    row = conn.execute(
        """
        SELECT * FROM email_otp_codes
        WHERE purpose=? AND identifier=? AND used_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (purpose, email),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    now = _utcnow()
    if datetime.fromisoformat(row["expires_at"]) < now:
        conn.execute(
            "UPDATE email_otp_codes SET used_at=? WHERE id=?",
            (now.isoformat(), row["id"]),
        )
        conn.commit()
        raise HTTPException(status_code=400, detail="OTP expired. Request a new OTP.")

    if int(row["attempts"] or 0) >= int(row["max_attempts"] or OTP_MAX_ATTEMPTS):
        conn.execute(
            "UPDATE email_otp_codes SET used_at=? WHERE id=?",
            (now.isoformat(), row["id"]),
        )
        conn.commit()
        raise HTTPException(status_code=400, detail="Too many incorrect attempts. Request a new OTP.")
    return row


def _verify_code(conn, row, purpose, email, otp):
    code = re.sub(r"\D", "", str(otp or ""))
    if len(code) != 6:
        raise HTTPException(status_code=400, detail="Enter the 6-digit OTP")

    supplied_hash = _otp_hash(purpose, email, row["nonce"], code)
    if not hmac.compare_digest(supplied_hash, row["code_hash"]):
        attempts = int(row["attempts"] or 0) + 1
        conn.execute(
            "UPDATE email_otp_codes SET attempts=? WHERE id=?",
            (attempts, row["id"]),
        )
        conn.commit()
        remaining = max(0, int(row["max_attempts"] or OTP_MAX_ATTEMPTS) - attempts)
        raise HTTPException(status_code=400, detail=f"Incorrect OTP. {remaining} attempt(s) remaining.")


def consume_registration_email_token(email, token, request):
    ensure_recovery_schema()
    email = _normalise_email(email)
    token = str(token or "").strip()
    if len(token) < 24:
        raise HTTPException(status_code=400, detail="Verify your email before registration")

    token_hash = _token_hash(email, token)
    now = _utcnow()
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT id, token_expires_at FROM email_otp_codes
            WHERE purpose='registration' AND identifier=?
              AND verification_token_hash=? AND verified_at IS NOT NULL
              AND consumed_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (email, token_hash),
        ).fetchone()
        if not row or not row["token_expires_at"]:
            raise HTTPException(status_code=400, detail="Verify your email before registration")
        if datetime.fromisoformat(row["token_expires_at"]) < now:
            raise HTTPException(status_code=400, detail="Email verification expired. Verify again.")

        cursor = conn.execute(
            """
            UPDATE email_otp_codes SET consumed_at=?
            WHERE id=? AND consumed_at IS NULL
            """,
            (now.isoformat(), row["id"]),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=400, detail="Email verification was already used")
        conn.commit()
    finally:
        conn.close()


class RegistrationEmailVerificationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method.upper() != "POST" or request.url.path.rstrip("/") != "/auth/register":
            return await call_next(request)

        body_bytes = await request.body()
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid registration request")

        email = payload.get("email")
        token = payload.get("email_verification_token")
        consume_registration_email_token(email, token, request)

        async def receive():
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        request._receive = receive
        return await call_next(request)


@router.get("/recovery-status")
def recovery_status():
    ensure_recovery_schema()
    return {
        "success": True,
        "email_otp_available": _email_service_available(),
        "mobile_otp_enabled": False,
        "otp_expiry_minutes": OTP_EXPIRY_MINUTES,
    }


@router.post("/request-email-verification")
def request_email_verification(body: EmailRequest, request: Request):
    ensure_recovery_schema()
    email = _normalise_email(body.email)
    if not _email_service_available():
        raise HTTPException(status_code=503, detail="Email OTP service is not configured yet")

    conn = get_db()
    try:
        event_id, _ = _rate_limit(conn, "request_email_verification", email, request)
        existing = conn.execute(
            "SELECT id FROM users WHERE email=? LIMIT 1",
            (email,),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Email already registered")

        _create_and_send_otp(
            conn,
            "registration",
            email,
            None,
            request,
            "Verify your Option King AI email",
            "Use this code to verify your email before creating your Option King AI account.",
        )
        _mark_event_success(conn, event_id)
        conn.commit()
        return {
            "success": True,
            "message": "A 6-digit verification OTP has been sent to your email.",
        }
    finally:
        conn.close()


@router.post("/verify-email-verification")
def verify_email_verification(body: VerifyEmailRequest, request: Request):
    ensure_recovery_schema()
    email = _normalise_email(body.email)
    conn = get_db()
    try:
        event_id, _ = _rate_limit(conn, "verify_email_verification", email, request)
        row = _get_valid_code(conn, "registration", email)
        _verify_code(conn, row, "registration", email, body.otp)

        now = _utcnow()
        token = secrets.token_urlsafe(32)
        token_hash = _token_hash(email, token)
        token_expires_at = now + timedelta(minutes=VERIFICATION_TOKEN_MINUTES)
        conn.execute(
            """
            UPDATE email_otp_codes
            SET used_at=?, verified_at=?, verification_token_hash=?, token_expires_at=?
            WHERE id=?
            """,
            (
                now.isoformat(),
                now.isoformat(),
                token_hash,
                token_expires_at.isoformat(),
                row["id"],
            ),
        )
        _mark_event_success(conn, event_id)
        conn.commit()
        return {
            "success": True,
            "email_verified": True,
            "email_verification_token": token,
            "message": "Email verified successfully.",
        }
    finally:
        conn.close()


@router.post("/recover-login-id")
def recover_login_id(body: RecoverLoginIdRequest, request: Request):
    ensure_recovery_schema()
    name = " ".join(str(body.name or "").strip().lower().split())
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Enter the registered full name")
    phone = _normalise_phone(body.phone)

    conn = get_db()
    try:
        event_id, _ = _rate_limit(conn, "recover_login_id", f"{name}|{phone}", request)
        user = conn.execute(
            """
            SELECT email FROM users
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

        _mark_event_success(conn, event_id)
        conn.commit()
        return {
            "success": True,
            "found": True,
            "masked_email": _mask_email(user["email"]),
            "message": "Your Login ID is the registered email shown below.",
        }
    finally:
        conn.close()


@router.post("/request-password-reset")
def request_password_reset(body: EmailRequest, request: Request):
    ensure_recovery_schema()
    email = _normalise_email(body.email)
    available = _email_service_available()
    generic = {
        "success": True,
        "email_otp_available": available,
        "message": (
            "If this email is registered, a password reset OTP has been sent."
            if available
            else "Email OTP service is not configured yet. Contact support."
        ),
    }

    conn = get_db()
    try:
        event_id, _ = _rate_limit(conn, "request_password_reset", email, request)
        user = conn.execute(
            "SELECT id, email, is_active FROM users WHERE email=? LIMIT 1",
            (email,),
        ).fetchone()
        if not available or not user or not user["is_active"]:
            return generic

        _create_and_send_otp(
            conn,
            "password_reset",
            email,
            user["id"],
            request,
            "Option King AI password reset OTP",
            "Use this code to reset your Option King AI password.",
        )
        _mark_event_success(conn, event_id)
        conn.commit()
        return generic
    finally:
        conn.close()


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, request: Request):
    ensure_recovery_schema()
    email = _normalise_email(body.email)
    new_password = str(body.new_password or "")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    conn = get_db()
    try:
        event_id, _ = _rate_limit(conn, "reset_password", email, request)
        user = conn.execute(
            "SELECT id, is_active FROM users WHERE email=? LIMIT 1",
            (email,),
        ).fetchone()
        if not user or not user["is_active"]:
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")

        row = _get_valid_code(conn, "password_reset", email)
        _verify_code(conn, row, "password_reset", email, body.otp)
        now = _utcnow().isoformat()
        conn.execute(
            "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
            (hash_password(new_password), now, user["id"]),
        )
        conn.execute(
            """
            UPDATE email_otp_codes SET used_at=?
            WHERE purpose='password_reset' AND identifier=? AND used_at IS NULL
            """,
            (now, email),
        )
        _mark_event_success(conn, event_id)
        conn.commit()
        return {
            "success": True,
            "message": "Password reset successfully. Login with the new password.",
        }
    finally:
        conn.close()
