import os
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException

from auth.routes import get_current_user
from database import get_db
from local_gateway.service import pair_gateway, get_gateway_status
from subscription.entitlements import entitlement_snapshot

router = APIRouter(prefix="/local-gateway/provision", tags=["Gateway Provisioning"])


def _now():
    return datetime.now(timezone.utc).isoformat()


def _manager_token(value):
    expected = str(os.getenv("OKAI_PROVISIONER_TOKEN") or "").strip()
    supplied = str(value or "").strip()
    if not expected or supplied != expected:
        raise HTTPException(status_code=401, detail="Invalid provisioner token")


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


@router.post("/request")
def request_gateway(authorization: str = Header(None)):
    user = dict(get_current_user(authorization))
    access = entitlement_snapshot(user)
    if not bool(user.get("is_admin")) and not bool(access.get("live_allowed")):
        raise HTTPException(status_code=403, detail="Live access is required before allocating a secure execution IP")
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
    return {"success": True, "provisioning": current, "gateway": get_gateway_status(user["id"])}


@router.get("/status")
def provisioning_status(authorization: str = Header(None)):
    user = get_current_user(authorization)
    ensure_schema()
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM gateway_provision_requests WHERE user_id=?", (int(user["id"]),)).fetchone()
    finally:
        conn.close()
    return {
        "success": True,
        "provisioning": dict(row) if row else {"state": "not_requested", "static_ip": None, "instance_id": None},
        "gateway": get_gateway_status(user["id"]),
    }


@router.get("/lease")
def lease_request(x_provisioner_token: str = Header(None)):
    _manager_token(x_provisioner_token)
    ensure_schema()
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
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
def allocate_callback(body: dict, x_provisioner_token: str = Header(None)):
    _manager_token(x_provisioner_token)
    ensure_schema()
    user_id = int(body.get("user_id") or 0)
    static_ip = str(body.get("static_ip") or "").strip()
    instance_id = str(body.get("instance_id") or "").strip()
    if user_id <= 0 or not static_ip:
        raise HTTPException(status_code=400, detail="user_id and static_ip are required")
    gateway_token = pair_gateway(user_id, "OKAI AWS Dedicated Gateway", static_ip)
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE gateway_provision_requests
            SET state='bootstrapping', static_ip=?, instance_id=?, last_error=NULL, updated_at=?
            WHERE user_id=?
            """,
            (static_ip, instance_id or None, _now(), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "gateway_token": gateway_token, "static_ip": static_ip}


@router.post("/ready")
def ready_callback(body: dict, x_provisioner_token: str = Header(None)):
    _manager_token(x_provisioner_token)
    ensure_schema()
    user_id = int(body.get("user_id") or 0)
    state = str(body.get("state") or "ready").strip().lower()
    if state not in {"ready", "error", "bootstrapping"}:
        state = "error"
    err = str(body.get("error") or "").strip()[:500] or None
    conn = get_db()
    try:
        conn.execute(
            "UPDATE gateway_provision_requests SET state=?, last_error=?, updated_at=? WHERE user_id=?",
            (state, err, _now(), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "state": state}
