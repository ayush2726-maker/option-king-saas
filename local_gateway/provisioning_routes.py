import os
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Header, HTTPException, Request

from auth.routes import get_current_user
from auth.utils import decrypt_credential
from broker.selection import get_selected_broker
from database import get_db
from local_gateway.service import authenticate_gateway, pair_gateway, get_gateway_status
from subscription.entitlements import entitlement_snapshot

router = APIRouter(prefix="/local-gateway/provision", tags=["Gateway Provisioning"])


def _now_dt():
    return datetime.now(timezone.utc)


def _now():
    return _now_dt().isoformat()


def _parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _client_ipv4(request: Request):
    real = str(request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    forwarded = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return str(request.client.host if request.client else "").strip()


def _manager_auth(request: Request, value):
    expected_token = str(os.getenv("OKAI_PROVISIONER_TOKEN") or "").strip()
    supplied = str(value or "").strip()
    if expected_token and supplied and supplied == expected_token:
        return
    expected_ip = str(os.getenv("OKAI_PROVISIONER_IP") or "").strip()
    if expected_ip and _client_ipv4(request) == expected_ip:
        return
    raise HTTPException(status_code=401, detail="Invalid provisioner identity")


def ensure_schema():
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gateway_provision_requests (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'requested',
                static_ip TEXT,
                instance_id TEXT,
                last_error TEXT,
                requested_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _selected_broker(user_id):
    conn = get_db()
    try:
        row = get_selected_broker(conn, int(user_id))
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _reconcile_ready(user_id):
    """Promote bootstrapping -> ready only after the dedicated worker is truly online."""
    ensure_schema()
    gateway = get_gateway_status(int(user_id))
    expected = str(gateway.get("expected_static_ip") or "").strip()
    observed = str(gateway.get("observed_ip") or "").strip()
    ip_matches = bool(expected and observed and expected == observed)
    ready = bool(
        gateway.get("paired")
        and gateway.get("enabled")
        and gateway.get("online")
        and ip_matches
    )
    if ready:
        conn = get_db()
        try:
            conn.execute(
                """
                UPDATE gateway_provision_requests
                SET state='ready', static_ip=COALESCE(static_ip, ?), last_error=NULL, updated_at=?
                WHERE user_id=? AND state IN ('allocating','bootstrapping','ready')
                """,
                (expected, _now(), int(user_id)),
            )
            conn.commit()
        finally:
            conn.close()
    gateway["static_ip_matches"] = ip_matches
    return gateway


@router.post("/request")
def request_gateway(authorization: str = Header(None)):
    user = dict(get_current_user(authorization))
    access = entitlement_snapshot(user)
    if not bool(user.get("is_admin")) and not bool(access.get("live_allowed")):
        raise HTTPException(status_code=403, detail="Live access is required before allocating a secure execution IP")
    broker = _selected_broker(user["id"])
    if not broker:
        raise HTTPException(status_code=409, detail="Connect and select a broker before secure IP allocation")
    broker_name = str(broker.get("broker_name") or "").strip().lower()
    if broker_name not in {"angelone", "upstox"}:
        raise HTTPException(status_code=409, detail="Dedicated cloud gateway currently supports Angel One and Upstox")

    ensure_schema()
    now = _now()
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM gateway_provision_requests WHERE user_id=?", (int(user["id"]),)).fetchone()
        if row and str(row["state"] or "") in {"requested", "allocating", "bootstrapping", "ready"}:
            current = dict(row)
        else:
            conn.execute(
                """
                INSERT INTO gateway_provision_requests(user_id,state,requested_at,updated_at)
                VALUES(?, 'requested', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    state='requested', last_error=NULL, requested_at=excluded.requested_at, updated_at=excluded.updated_at
                """,
                (int(user["id"]), now, now),
            )
            conn.commit()
            current = dict(conn.execute("SELECT * FROM gateway_provision_requests WHERE user_id=?", (int(user["id"]),)).fetchone())
    finally:
        conn.close()
    return {"success": True, "broker": broker_name, "provisioning": current, "gateway": _reconcile_ready(user["id"])}


@router.get("/status")
def provisioning_status(authorization: str = Header(None)):
    user = get_current_user(authorization)
    ensure_schema()
    gateway = _reconcile_ready(user["id"])
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM gateway_provision_requests WHERE user_id=?", (int(user["id"]),)).fetchone()
    finally:
        conn.close()
    return {
        "success": True,
        "provisioning": dict(row) if row else {"state": "not_requested", "static_ip": None, "instance_id": None},
        "gateway": gateway,
    }


@router.get("/lease")
def lease_request(request: Request, x_provisioner_token: str = Header(None)):
    _manager_auth(request, x_provisioner_token)
    ensure_schema()
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stale_before = (_now_dt() - timedelta(minutes=8)).isoformat()
        conn.execute(
            """
            UPDATE gateway_provision_requests
            SET state='requested', last_error='Provisioning retry after stale allocation lease', updated_at=?
            WHERE state='allocating' AND updated_at < ?
            """,
            (_now(), stale_before),
        )
        row = conn.execute(
            """
            SELECT * FROM gateway_provision_requests
            WHERE state='requested'
            ORDER BY requested_at ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.commit()
            return {"success": True, "job": None}
        conn.execute(
            "UPDATE gateway_provision_requests SET state='allocating', updated_at=? WHERE user_id=?",
            (_now(), int(row["user_id"])),
        )
        conn.commit()
        return {"success": True, "job": {"user_id": int(row["user_id"])}}
    finally:
        conn.close()


@router.post("/allocate")
def allocate_callback(request: Request, body: dict, x_provisioner_token: str = Header(None)):
    _manager_auth(request, x_provisioner_token)
    ensure_schema()
    user_id = int(body.get("user_id") or 0)
    static_ip = str(body.get("static_ip") or "").strip()
    instance_id = str(body.get("instance_id") or "").strip()
    if user_id <= 0 or not static_ip:
        raise HTTPException(status_code=400, detail="user_id and static_ip are required")

    # Pair exactly once for a new allocation. Repeated manager retries must not
    # rotate a live worker's token.
    current_gateway = get_gateway_status(user_id)
    already_paired_for_ip = bool(
        current_gateway.get("paired")
        and str(current_gateway.get("expected_static_ip") or "").strip() == static_ip
    )
    gateway_token = None
    if not already_paired_for_ip:
        gateway_token = pair_gateway(user_id, "OKAI AWS Dedicated Gateway", static_ip)

    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE gateway_provision_requests
            SET state='bootstrapping', static_ip=?, instance_id=COALESCE(NULLIF(?,''), instance_id), last_error=NULL, updated_at=?
            WHERE user_id=?
            """,
            (static_ip, instance_id, _now(), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "success": True,
        "gateway_token": gateway_token,
        "token_already_active": already_paired_for_ip,
        "static_ip": static_ip,
    }


@router.post("/ready")
def ready_callback(request: Request, body: dict, x_provisioner_token: str = Header(None)):
    _manager_auth(request, x_provisioner_token)
    ensure_schema()
    user_id = int(body.get("user_id") or 0)
    state = str(body.get("state") or "ready").strip().lower()
    if state not in {"ready", "error", "bootstrapping"}:
        state = "error"
    err = str(body.get("error") or "").strip()[:500] or None
    instance_id = str(body.get("instance_id") or "").strip()
    static_ip = str(body.get("static_ip") or "").strip()
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE gateway_provision_requests
            SET state=?, last_error=?,
                instance_id=COALESCE(NULLIF(?,''), instance_id),
                static_ip=COALESCE(NULLIF(?,''), static_ip),
                updated_at=?
            WHERE user_id=?
            """,
            (state, err, instance_id, static_ip, _now(), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "state": state}


@router.get("/bootstrap")
def gateway_bootstrap(x_gateway_token: str = Header(None)):
    """Private worker-only bootstrap. Never expose this route through customer UI."""
    gateway = authenticate_gateway(x_gateway_token)
    user_id = int(gateway["user_id"])
    conn = get_db()
    try:
        cred = get_selected_broker(conn, user_id)
        if cred is None:
            raise HTTPException(status_code=409, detail="No selected broker credentials")
        broker_name = str(cred["broker_name"] or "").strip().lower()
        api_key = decrypt_credential(cred["api_key"]) if cred["api_key"] else ""
        api_secret = decrypt_credential(cred["api_secret"]) if cred["api_secret"] else ""
        totp = decrypt_credential(cred["totp_secret"]) if cred["totp_secret"] else ""
        client_id = str(cred["client_id"] or "").strip()
    finally:
        conn.close()

    expected_ip = str(gateway["expected_static_ip"] or "").strip()
    base = {
        "success": True,
        "user_id": user_id,
        "broker": broker_name,
        "expected_static_ip": expected_ip,
        "device_name": "OKAI AWS Dedicated Gateway",
    }
    if broker_name == "angelone":
        if not all([client_id, api_key, api_secret, totp]):
            raise HTTPException(status_code=409, detail="Angel One credentials are incomplete")
        base["agent_script"] = "okai_local_gateway_v3.py"
        base["broker_config"] = {
            "api_key": api_key,
            "client_id": client_id,
            "password": api_secret,
            "totp_secret": totp,
        }
        return base
    if broker_name == "upstox":
        if not api_secret:
            raise HTTPException(status_code=409, detail="Upstox access token is missing")
        base["agent_script"] = "okai_local_gateway_upstox.py"
        base["broker_config"] = {"access_token": api_secret}
        return base
    raise HTTPException(status_code=409, detail="Selected broker is not supported by cloud gateway")
