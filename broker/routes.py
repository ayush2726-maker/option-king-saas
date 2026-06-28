from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from datetime import datetime
from database import get_db
from auth.routes import get_current_user
from auth.utils import encrypt_credential, decrypt_credential
from bot.brokers.factory import create_broker, get_all_brokers_info, get_broker_info, get_supported_brokers

router = APIRouter(prefix="/broker", tags=["Broker"])

class BrokerConnectRequest(BaseModel):
    broker_name: str
    client_id: str
    api_key: str
    api_secret: str
    totp_secret: str = None

@router.get("/supported")
def list_supported_brokers():
    return {"success": True, "brokers": get_all_brokers_info()}

@router.get("/info/{broker_name}")
def get_single_broker_info(broker_name: str):
    info = get_broker_info(broker_name)
    if "error" in info:
        raise HTTPException(status_code=404, detail=info["error"])
    return {"success": True, "broker": info}

@router.get("/setup-guide/{broker_name}")
def get_setup_guide(broker_name: str):
    info = get_broker_info(broker_name)
    if "error" in info:
        raise HTTPException(status_code=404, detail=info["error"])
    return {"success": True, "setup_guide": info["setup_guide"], "required_fields": info["required_fields"]}

@router.post("/connect")
def connect_broker(req: BrokerConnectRequest, authorization: str = Header(None)):
    user = get_current_user(authorization)
    if user["subscription_status"] not in ("trial", "active"):
        raise HTTPException(status_code=403, detail="Active subscription required")
    broker_name = req.broker_name.lower().strip()
    if broker_name not in get_supported_brokers():
        raise HTTPException(status_code=400, detail=f"Unsupported broker. Supported: {get_supported_brokers()}")
    try:
        broker = create_broker(broker_name, req.client_id, req.api_key, req.api_secret, req.totp_secret)
        login_result = broker.login()
        if not login_result["success"]:
            raise HTTPException(status_code=400, detail=f"Broker login failed: {login_result['message']}")
    except HTTPException:
        raise
    except Exception:
        pass
    enc_api_key = encrypt_credential(req.api_key)
    enc_api_secret = encrypt_credential(req.api_secret)
    enc_totp = encrypt_credential(req.totp_secret) if req.totp_secret else None
    now = datetime.utcnow().isoformat()
    conn = get_db()
    existing = conn.execute("SELECT id FROM broker_credentials WHERE user_id=? AND broker_name=?", (user["id"], broker_name)).fetchone()
    if existing:
        conn.execute("UPDATE broker_credentials SET client_id=?, api_key=?, api_secret=?, totp_secret=?, is_active=1, last_connected=? WHERE id=?", (req.client_id, enc_api_key, enc_api_secret, enc_totp, now, existing["id"]))
        msg = f"{broker_name.title()} credentials updated"
    else:
        conn.execute("INSERT INTO broker_credentials (user_id, broker_name, client_id, api_key, api_secret, totp_secret, last_connected) VALUES (?,?,?,?,?,?,?)", (user["id"], broker_name, req.client_id, enc_api_key, enc_api_secret, enc_totp, now))
        msg = f"{broker_name.title()} connected successfully"
    conn.commit()
    conn.close()
    return {"success": True, "message": msg, "broker": broker_name, "client_id": req.client_id}

@router.get("/list")
def list_brokers(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    brokers = conn.execute("SELECT id, broker_name, client_id, is_active, last_connected, created_at FROM broker_credentials WHERE user_id=?", (user["id"],)).fetchall()
    conn.close()
    return {"success": True, "brokers": [dict(b) for b in brokers]}

@router.get("/test/{broker_name}")
def test_broker_connection(broker_name: str, authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    cred = conn.execute("SELECT * FROM broker_credentials WHERE user_id=? AND broker_name=?", (user["id"], broker_name.lower())).fetchone()
    conn.close()
    if not cred:
        raise HTTPException(status_code=404, detail="Broker not connected")
    api_key = decrypt_credential(cred["api_key"])
    api_secret = decrypt_credential(cred["api_secret"])
    totp = decrypt_credential(cred["totp_secret"]) if cred["totp_secret"] else None
    try:
        broker = create_broker(broker_name, cred["client_id"], api_key, api_secret, totp)
        result = broker.login()
        if result["success"]:
            funds = broker.get_funds()
            return {"success": True, "broker": broker_name, "status": "connected", "funds": funds}
        return {"success": False, "broker": broker_name, "status": "auth_failed", "message": result["message"]}
    except Exception as e:
        return {"success": False, "broker": broker_name, "status": "error", "message": str(e)}

@router.delete("/disconnect/{broker_name}")
def disconnect_broker(broker_name: str, authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    result = conn.execute("DELETE FROM broker_credentials WHERE user_id=? AND broker_name=?", (user["id"], broker_name.lower()))
    conn.commit()
    conn.close()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Broker not found")
    return {"success": True, "message": f"{broker_name} disconnected"}
