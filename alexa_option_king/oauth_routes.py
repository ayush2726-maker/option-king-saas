from __future__ import annotations

import base64
import hashlib
import html
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth.utils import create_access_token, verify_password
from database import get_db

router = APIRouter(prefix="/alexa/oauth", tags=["Alexa OAuth"])

CLIENT_ID = os.getenv("ALEXA_OAUTH_CLIENT_ID", "option-king-alexa")
CLIENT_SECRET = os.getenv("ALEXA_OAUTH_CLIENT_SECRET", "")
AUTH_CODE_MINUTES = 5
REFRESH_DAYS = 180
ACCESS_TOKEN_SECONDS = 24 * 60 * 60


def _now() -> datetime:
    return datetime.utcnow()


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _ensure_schema() -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alexa_oauth_codes (
                code_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                redirect_uri TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alexa_oauth_refresh_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_redirect_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    allowed_hosts = {
        "layla.amazon.com",
        "pitangui.amazon.com",
        "alexa.amazon.co.jp",
    }
    return host in allowed_hosts and parsed.path.startswith("/api/skill/link/")


def _redirect_with_error(redirect_uri: str, state: str, error: str, description: str = ""):
    params = {"error": error}
    if state:
        params["state"] = state
    if description:
        params["error_description"] = description
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=303)


def _login_page(*, client_id: str, redirect_uri: str, state: str, scope: str, error: str = "", email: str = "") -> HTMLResponse:
    safe_error = html.escape(error)
    error_box = f'<div class="err">{safe_error}</div>' if safe_error else ""
    return HTMLResponse(
        f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link Option King AI</title>
<style>
body{{margin:0;background:#0d1320;color:#f4f7fb;font-family:Arial,sans-serif;padding:28px}}
.card{{max-width:430px;margin:8vh auto;background:#151e2f;padding:28px;border-radius:20px;box-shadow:0 16px 50px #0006}}
h1{{margin:0 0 8px}} p{{color:#b8c2d1;line-height:1.45}} label{{display:block;margin:18px 0 7px;font-weight:700}}
input{{box-sizing:border-box;width:100%;padding:14px;border-radius:11px;border:1px solid #3a465c;background:#0e1625;color:white;font-size:16px}}
button{{width:100%;margin-top:22px;padding:15px;border:0;border-radius:11px;background:#18a86b;color:white;font-weight:800;font-size:16px}}
.err{{background:#5b1b24;color:#ffd7dc;padding:12px;border-radius:10px;margin:14px 0}}
.small{{font-size:12px;color:#8e9aae}}
</style></head><body><div class="card">
<h1>Option King AI</h1><p>Sign in to link this Alexa account. Alexa will only access the linked user's read-only Option King information.</p>{error_box}
<form method="post" action="/alexa/oauth/authorize">
<input type="hidden" name="client_id" value="{html.escape(client_id, quote=True)}">
<input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri, quote=True)}">
<input type="hidden" name="state" value="{html.escape(state, quote=True)}">
<input type="hidden" name="scope" value="{html.escape(scope, quote=True)}">
<label>Email</label><input name="email" type="email" autocomplete="username" value="{html.escape(email, quote=True)}" required>
<label>Password</label><input name="password" type="password" autocomplete="current-password" required>
<button type="submit">Link Alexa</button>
</form><p class="small">Your password is verified directly by Option King and is not shared with Amazon.</p>
</div></body></html>""",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/authorize")
def authorize_get(client_id: str, redirect_uri: str, state: str = "", scope: str = "alexa", response_type: str = "code"):
    if response_type != "code":
        raise HTTPException(status_code=400, detail="unsupported response_type")
    if client_id != CLIENT_ID:
        raise HTTPException(status_code=400, detail="invalid client_id")
    if not _valid_redirect_uri(redirect_uri):
        raise HTTPException(status_code=400, detail="invalid redirect_uri")
    return _login_page(client_id=client_id, redirect_uri=redirect_uri, state=state, scope=scope)


@router.post("/authorize")
async def authorize_post(request: Request):
    body = (await request.body()).decode("utf-8", errors="replace")
    form = parse_qs(body, keep_blank_values=True)
    value = lambda key, default="": (form.get(key, [default])[0] or default).strip()
    client_id = value("client_id")
    redirect_uri = value("redirect_uri")
    state = value("state")
    scope = value("scope", "alexa")
    email = value("email").lower()
    password = value("password")

    if client_id != CLIENT_ID or not _valid_redirect_uri(redirect_uri):
        raise HTTPException(status_code=400, detail="invalid OAuth request")

    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
    finally:
        conn.close()
    if not user or not verify_password(password, user["password_hash"]):
        return _login_page(client_id=client_id, redirect_uri=redirect_uri, state=state, scope=scope, error="Invalid email or password.", email=email)
    if not bool(user["is_active"]):
        return _login_page(client_id=client_id, redirect_uri=redirect_uri, state=state, scope=scope, error="This account is suspended.", email=email)

    _ensure_schema()
    code = secrets.token_urlsafe(40)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO alexa_oauth_codes(code_hash,user_id,redirect_uri,expires_at,created_at) VALUES(?,?,?,?,?)",
            (_hash(code), int(user["id"]), redirect_uri, _iso(_now() + timedelta(minutes=AUTH_CODE_MINUTES)), _iso(_now())),
        )
        conn.commit()
    finally:
        conn.close()

    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=303)


def _client_credentials(request: Request, form: dict[str, list[str]]) -> tuple[str, str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            return tuple(raw.split(":", 1))  # type: ignore[return-value]
        except Exception:
            return "", ""
    return (form.get("client_id", [""])[0], form.get("client_secret", [""])[0])


def _verify_client(client_id: str, secret: str) -> None:
    if not CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="OAuth client secret is not configured on server")
    if not secrets.compare_digest(client_id, CLIENT_ID) or not secrets.compare_digest(secret, CLIENT_SECRET):
        raise HTTPException(status_code=401, detail="invalid_client")


def _issue_tokens(user_id: int) -> dict:
    conn = get_db()
    try:
        user = conn.execute("SELECT id,email,is_active FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or not bool(user["is_active"]):
            raise HTTPException(status_code=401, detail="invalid user")
        access_token = create_access_token(int(user["id"]), str(user["email"]))
        refresh_token = secrets.token_urlsafe(48)
        conn.execute(
            "INSERT INTO alexa_oauth_refresh_tokens(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
            (_hash(refresh_token), int(user["id"]), _iso(_now() + timedelta(days=REFRESH_DAYS)), _iso(_now())),
        )
        conn.commit()
    finally:
        conn.close()
    return {"access_token": access_token, "token_type": "Bearer", "expires_in": ACCESS_TOKEN_SECONDS, "refresh_token": refresh_token, "scope": "alexa"}


@router.post("/token")
async def token(request: Request):
    _ensure_schema()
    body = (await request.body()).decode("utf-8", errors="replace")
    form = parse_qs(body, keep_blank_values=True)
    client_id, secret = _client_credentials(request, form)
    _verify_client(client_id, secret)
    grant_type = (form.get("grant_type", [""])[0] or "").strip()

    if grant_type == "authorization_code":
        code = (form.get("code", [""])[0] or "").strip()
        redirect_uri = (form.get("redirect_uri", [""])[0] or "").strip()
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM alexa_oauth_codes WHERE code_hash=?", (_hash(code),)).fetchone()
            if not row or row["used_at"] or row["expires_at"] <= _iso(_now()) or row["redirect_uri"] != redirect_uri:
                raise HTTPException(status_code=400, detail="invalid_grant")
            conn.execute("UPDATE alexa_oauth_codes SET used_at=? WHERE code_hash=?", (_iso(_now()), _hash(code)))
            conn.commit()
            user_id = int(row["user_id"])
        finally:
            conn.close()
        return JSONResponse(_issue_tokens(user_id), headers={"Cache-Control": "no-store"})

    if grant_type == "refresh_token":
        old_token = (form.get("refresh_token", [""])[0] or "").strip()
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM alexa_oauth_refresh_tokens WHERE token_hash=?", (_hash(old_token),)).fetchone()
            if not row or row["revoked_at"] or row["expires_at"] <= _iso(_now()):
                raise HTTPException(status_code=400, detail="invalid_grant")
            conn.execute("UPDATE alexa_oauth_refresh_tokens SET revoked_at=? WHERE token_hash=?", (_iso(_now()), _hash(old_token)))
            conn.commit()
            user_id = int(row["user_id"])
        finally:
            conn.close()
        return JSONResponse(_issue_tokens(user_id), headers={"Cache-Control": "no-store"})

    raise HTTPException(status_code=400, detail="unsupported_grant_type")


@router.get("/health")
def oauth_health():
    return {"status": "ok", "client_id": CLIENT_ID, "client_secret_configured": bool(CLIENT_SECRET)}
