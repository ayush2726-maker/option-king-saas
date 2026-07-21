import ipaddress

from fastapi import APIRouter, Header, HTTPException, Request

from auth.routes import get_current_user
from local_gateway.multi_user_patch import (
    PATCH_VERSION,
    apply_multi_user_gateway_patch,
    gateway_access,
    multi_user_enabled,
)

# Apply before importing service functions so this router receives the patched
# multi-user-safe implementations rather than the legacy owner-only aliases.
apply_multi_user_gateway_patch()

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


def _valid_ipv4(value):
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return ""
    return str(parsed) if parsed.version == 4 else ""


def _observed_client_ip(request: Request):
    """Return the real public IPv4 added by Railway's trusted edge proxy."""
    railway_ip = _valid_ipv4(request.headers.get("x-real-ip"))
    if railway_ip:
        return railway_ip

    forwarded = str(request.headers.get("x-forwarded-for") or "")
    for value in forwarded.split(","):
        candidate = _valid_ipv4(value)
        if candidate:
            return candidate

    return _valid_ipv4(request.client.host if request.client else "")


def _setup_guide():
    return {
        "requirements": [
            "Your own Angel One account with F&O enabled",
            "Your own SmartAPI app registered with your public static IPv4",
            "An always-on Android/Termux phone or Windows/Linux desktop on that connection",
            "Your own OKAI login; never share API key, MPIN, password or TOTP secret",
        ],
        "termux_commands": [
            "pkg update -y && pkg install python git tmux -y",
            "git clone https://github.com/ayush2726-maker/option-king-saas.git",
            "cd option-king-saas/local_gateway_agent",
            "pip install -r requirements.txt",
            "python okai_local_gateway_v2.py setup",
            "python okai_local_gateway_v2.py doctor",
            "python okai_local_gateway_v2.py run",
        ],
        "desktop_commands": [
            "git clone https://github.com/ayush2726-maker/option-king-saas.git",
            "cd option-king-saas/local_gateway_agent",
            "python -m venv .venv",
            "Activate .venv, then: python -m pip install -r requirements.txt",
            "python okai_local_gateway_v2.py setup",
            "python okai_local_gateway_v2.py doctor",
            "python okai_local_gateway_v2.py run",
        ],
        "live_safety": [
            "Setup and doctor do not arm live orders",
            "Arm only on the gateway device with the exact phrase ARM LIVE 1 LOT",
            "Every Risk V2 entry is hard-clamped to one lot locally",
            "Disarming blocks new entries while existing positions remain monitored",
        ],
    }


@router.get("/access")
def local_gateway_access(authorization: str = Header(None)):
    user = get_current_user(authorization)
    access = gateway_access(user)
    return {
        "success": True,
        "feature": "multi_user_local_static_ip_gateway",
        "policy_version": PATCH_VERSION,
        "multi_user_enabled": multi_user_enabled(),
        "access": access,
        "gateway": get_gateway_status(user["id"]),
        "setup": _setup_guide(),
    }


@router.post("/pair")
def pair_local_gateway(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    require_personal_user(user)
    expected_ip = _valid_ipv4(body.get("expected_static_ip"))
    if not expected_ip:
        raise HTTPException(
            status_code=400,
            detail="A valid registered public static IPv4 is required",
        )
    token = pair_gateway(
        user["id"],
        body.get("device_name") or "OKAI Gateway Device",
        expected_ip,
    )
    return {
        "success": True,
        "message": "Local gateway paired. Token is shown only once.",
        "gateway_token": token,
        "server_armed": False,
        "user_id": user["id"],
        "expected_static_ip": expected_ip,
        "policy_version": PATCH_VERSION,
    }


@router.get("/status")
def local_gateway_status(authorization: str = Header(None)):
    user = get_current_user(authorization)
    return {
        "success": True,
        "access": gateway_access(user),
        **get_gateway_status(user["id"]),
    }


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
    # Disarm is always allowed, including after a subscription expires.
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
    observed_ip = _observed_client_ip(request)
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
    observed_ip = _observed_client_ip(request)
    heartbeat = heartbeat_gateway(gateway, observed_ip, "poll")
    expected_ip = str(heartbeat.get("expected_static_ip") or "").strip()
    ip_allowed = (
        bool(heartbeat.get("static_ip_matches"))
        if expected_ip
        else False
    )
    access_allowed = bool(heartbeat.get("gateway_access_allowed"))
    allow_entries = (
        bool(heartbeat.get("server_armed"))
        and ip_allowed
        and access_allowed
    )
    commands = lease_commands(
        gateway,
        limit,
        allow_entries=allow_entries,
    )
    return {
        "success": True,
        "server_armed": bool(heartbeat.get("server_armed")),
        "static_ip_matches": bool(heartbeat.get("static_ip_matches")),
        "expected_static_ip": heartbeat.get("expected_static_ip"),
        "observed_ip": heartbeat.get("observed_ip"),
        "gateway_access_allowed": access_allowed,
        "gateway_access_reason": heartbeat.get("gateway_access_reason"),
        "entry_commands_allowed": allow_entries,
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
    # Closing a user's own position is always allowed, even after expiry.
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
