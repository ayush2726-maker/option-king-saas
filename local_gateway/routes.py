from fastapi import APIRouter, Header, HTTPException, Request

from auth.routes import get_current_user
from local_gateway.service import (
    authenticate_gateway,
    complete_command,
    get_gateway_status,
    heartbeat_gateway,
    lease_commands,
    pair_gateway,
    queue_exit,
    record_position_event,
    require_personal_user,
    set_gateway_armed,
)


router = APIRouter(prefix="/local-gateway", tags=["Local Static IP Gateway"])


def _gateway_token(value):
    token = str(value or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing X-Gateway-Token")
    return token


@router.post("/pair")
def pair_local_gateway(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    require_personal_user(user)
    token = pair_gateway(
        user["id"],
        body.get("device_name") or "OKAI Server Phone",
        body.get("expected_static_ip") or "",
    )
    return {
        "success": True,
        "message": "Local gateway paired. Token is shown only once.",
        "gateway_token": token,
        "server_armed": False,
    }


@router.get("/status")
def local_gateway_status(authorization: str = Header(None)):
    user = get_current_user(authorization)
    require_personal_user(user)
    return {"success": True, **get_gateway_status(user["id"])}


@router.post("/arm")
def arm_local_gateway(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    require_personal_user(user)
    confirmation = str(body.get("confirmation") or "").strip().upper()
    if confirmation != "ARM LIVE ORDERS":
        raise HTTPException(
            status_code=400,
            detail="Type exact confirmation: ARM LIVE ORDERS",
        )
    set_gateway_armed(user["id"], True)
    return {
        "success": True,
        "server_armed": True,
        "message": "Server-side live order gateway armed",
    }


@router.post("/disarm")
def disarm_local_gateway(authorization: str = Header(None)):
    user = get_current_user(authorization)
    require_personal_user(user)
    set_gateway_armed(user["id"], False)
    return {
        "success": True,
        "server_armed": False,
        "message": "New live entries are disarmed. Existing local positions remain monitored.",
    }


@router.post("/gateway-arm")
def gateway_arm(body: dict, x_gateway_token: str = Header(None)):
    gateway = authenticate_gateway(_gateway_token(x_gateway_token))
    confirmation = str(body.get("confirmation") or "").strip().upper()
    armed = bool(body.get("armed"))
    if armed and confirmation != "ARM LIVE ORDERS":
        raise HTTPException(
            status_code=400,
            detail="Type exact confirmation: ARM LIVE ORDERS",
        )
    set_gateway_armed(gateway["user_id"], armed)
    return {
        "success": True,
        "server_armed": armed,
        "message": "Gateway armed" if armed else "Gateway disarmed",
    }


@router.post("/heartbeat")
def gateway_heartbeat(
    body: dict,
    request: Request,
    x_gateway_token: str = Header(None),
):
    gateway = authenticate_gateway(_gateway_token(x_gateway_token))
    observed_ip = request.client.host if request.client else ""
    status = heartbeat_gateway(
        gateway,
        observed_ip,
        body.get("agent_version") or "",
    )
    return {"success": True, **status}


@router.get("/poll")
def gateway_poll(
    request: Request,
    limit: int = 5,
    x_gateway_token: str = Header(None),
):
    gateway = authenticate_gateway(_gateway_token(x_gateway_token))
    observed_ip = request.client.host if request.client else ""
    heartbeat_gateway(gateway, observed_ip, "poll")
    commands = lease_commands(gateway, limit)
    return {
        "success": True,
        "server_armed": bool(gateway["server_armed"]),
        "commands": commands,
    }


@router.post("/commands/{command_id}/result")
def gateway_command_result(
    command_id: int,
    body: dict,
    x_gateway_token: str = Header(None),
):
    gateway = authenticate_gateway(_gateway_token(x_gateway_token))
    return {
        "success": True,
        **complete_command(
            gateway,
            command_id,
            body.get("lease_token") or "",
            bool(body.get("success")),
            body.get("result") or {},
            body.get("error") or "",
        ),
    }


@router.post("/position-event")
def gateway_position_event(body: dict, x_gateway_token: str = Header(None)):
    gateway = authenticate_gateway(_gateway_token(x_gateway_token))
    return {"success": True, **record_position_event(gateway, body)}


@router.post("/exit-now")
def exit_live_position(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    require_personal_user(user)
    trade_id = int(body.get("trade_id") or 0)
    if trade_id <= 0:
        raise HTTPException(status_code=400, detail="Valid trade_id is required")
    result = queue_exit(
        user["id"],
        trade_id,
        body.get("reason") or "MANUAL EXIT FROM APP",
    )
    if not result.get("queued"):
        raise HTTPException(status_code=409, detail=result.get("reason"))
    return {"success": True, **result}
