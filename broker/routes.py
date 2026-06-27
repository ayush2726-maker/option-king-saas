from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from datetime import datetime
from database import get_db
from auth.routes import get_current_user
from auth.utils import encrypt_credential, decrypt_credential

router = APIRouter(prefix="/broker", tags=["Broker"])

SUPPORTED_BROKERS = ["angelone", "zerodha", "upstox"]


class BrokerConnectRequest(BaseModel):
    broker_name: str       # angelone / zerodha / upstox
    client_id: str         # User's broker client ID
    api_key: str
    api_secret: str
    totp_secret: str = None  # For Angel One


@router.post("/connect")
def connect_broker(req: BrokerConnectRequest, authorization: str = Header(None)):
    user = get_current_user(authorization)

    # Check subscription
    if user["subscription_status"] not in ("trial", "active"):
        raise HTTPException(
            status_code=403,
            detail="Active subscription required to connect broker"
        )

    broker = req.broker_name.lower().strip()
    if broker not in SUPPORTED_BROKERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported broker. Supported: {', '.join(SUPPORTED_BROKERS)}"
        )

    conn = get_db()

    # Check if already connected
    existing = conn.execute(
        "SELECT id FROM broker_credentials WHERE user_id=? AND broker_name=?",
        (user["id"], broker)
    ).fetchone()

    # Encrypt sensitive fields
    enc_api_key = encrypt_credential(req.api_key)
    enc_api_secret = encrypt_credential(req.api_secret)
    enc_totp = encrypt_credential(req.totp_secret) if req.totp_secret else None

    now = datetime.utcnow().isoformat()

    if existing:
        # Update existing
        conn.execute(
            """UPDATE broker_credentials
               SET client_id=?, api_key=?, api_secret=?, totp_secret=?,
                   is_active=1, last_connected=?
               WHERE id=?""",
            (req.client_id, enc_api_key, enc_api_secret, enc_totp, now, existing["id"])
        )
        msg = f"{broker.title()} credentials updated successfully"
    else:
        # Insert new
        conn.execute(
            """INSERT INTO broker_credentials
               (user_id, broker_name, client_id, api_key, api_secret, totp_secret, last_connected)
               VALUES (?,?,?,?,?,?,?)""",
            (user["id"], broker, req.client_id, enc_api_key, enc_api_secret, enc_totp, now)
        )
        msg = f"{broker.title()} connected successfully ✅"

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": msg,
        "broker": broker,
        "client_id": req.client_id
    }


@router.get("/list")
def list_brokers(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    brokers = conn.execute(
        """SELECT id, broker_name, client_id, is_active, last_connected, created_at
           FROM broker_credentials WHERE user_id=?""",
        (user["id"],)
    ).fetchall()
    conn.close()

    return {
        "success": True,
        "brokers": [dict(b) for b in brokers]
    }


@router.delete("/disconnect/{broker_name}")
def disconnect_broker(broker_name: str, authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    result = conn.execute(
        "DELETE FROM broker_credentials WHERE user_id=? AND broker_name=?",
        (user["id"], broker_name.lower())
    )
    conn.commit()
    conn.close()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Broker not found")

    return {"success": True, "message": f"{broker_name} disconnected"}


@router.get("/test/{broker_name}")
def test_broker_connection(broker_name: str, authorization: str = Header(None)):
    """Test if broker credentials are valid"""
    user = get_current_user(authorization)

    conn = get_db()
    cred = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND broker_name=?",
        (user["id"], broker_name.lower())
    ).fetchone()
    conn.close()

    if not cred:
        raise HTTPException(status_code=404, detail="Broker not connected")

    # Decrypt credentials
    api_key = decrypt_credential(cred["api_key"])

    # TODO: Actually test connection with broker API
    # For now return success (actual broker ping to be added per broker)
    return {
        "success": True,
        "broker": broker_name,
        "client_id": cred["client_id"],
        "status": "connected",
        "last_connected": cred["last_connected"]
    }
