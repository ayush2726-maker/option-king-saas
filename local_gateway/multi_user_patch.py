"""Multi-user access policy for the local static-IP order gateway.

Every OKAI user keeps a separate gateway token, expected public IPv4, command
queue and trade rows.  New live entries require an active/trial subscription
and the registration-time automated-order acknowledgement.  Gateway tokens
remain usable for heartbeat/exit handling after expiry so an already-open
position is never stranded.
"""

import os
from datetime import datetime, timezone

from fastapi import HTTPException

from database import get_db
from local_gateway import service


PATCH_VERSION = "MULTI_USER_LOCAL_GATEWAY_V1"


def _env_bool(name, default):
    value = str(os.getenv(name, "true" if default else "false")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def multi_user_enabled():
    return _env_bool("LOCAL_GATEWAY_MULTI_USER_ENABLED", True)


def admin_only_enabled():
    # Explicit Railway override can restore owner-only mode immediately.
    return _env_bool("LOCAL_GATEWAY_ADMIN_ONLY", False)


def _parse_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _load_user(user_or_id):
    if isinstance(user_or_id, dict):
        return dict(user_or_id)
    try:
        user_id = int(user_or_id)
    except Exception:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT id, email, is_active, is_admin,
                   subscription_status, trial_ends_at
            FROM users WHERE id=?
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _algo_consent(user_id):
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT algo_order_authorized, accepted_at, policy_version
            FROM user_consents
            WHERE user_id=?
            ORDER BY datetime(accepted_at) DESC, id DESC
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {
            "verified": False,
            "authorized": False,
            "accepted_at": None,
            "policy_version": None,
        }
    return {
        "verified": True,
        "authorized": bool(row["algo_order_authorized"]),
        "accepted_at": row["accepted_at"],
        "policy_version": row["policy_version"],
    }


def gateway_access(user_or_id):
    user = _load_user(user_or_id)
    if not user:
        return {
            "allowed": False,
            "reason": "USER_NOT_FOUND",
            "message": "User account was not found",
        }

    if not bool(user.get("is_active")):
        return {
            "allowed": False,
            "reason": "ACCOUNT_SUSPENDED",
            "message": "Account is suspended",
        }

    if bool(user.get("is_admin")):
        return {
            "allowed": True,
            "reason": "ADMIN_UNLIMITED",
            "message": "Owner/admin gateway access is enabled",
            "subscription_status": "active",
            "consent_verified": True,
            "multi_user_enabled": multi_user_enabled(),
            "admin_only": admin_only_enabled(),
        }

    if admin_only_enabled():
        return {
            "allowed": False,
            "reason": "OWNER_ONLY_MODE",
            "message": "Local gateway is temporarily restricted to the owner/admin",
            "multi_user_enabled": multi_user_enabled(),
            "admin_only": True,
        }

    if not multi_user_enabled():
        return {
            "allowed": False,
            "reason": "MULTI_USER_GATEWAY_DISABLED",
            "message": "Multi-user local gateway access is currently disabled",
            "multi_user_enabled": False,
            "admin_only": False,
        }

    status = str(user.get("subscription_status") or "").strip().lower()
    subscription_allowed = status == "active"
    if status == "trial":
        trial_end = _parse_datetime(user.get("trial_ends_at"))
        subscription_allowed = bool(trial_end and trial_end > datetime.now(timezone.utc))

    if not subscription_allowed:
        return {
            "allowed": False,
            "reason": "SUBSCRIPTION_REQUIRED",
            "message": "An active subscription or valid trial is required for new live entries",
            "subscription_status": status or "expired",
            "multi_user_enabled": True,
            "admin_only": False,
        }

    consent = _algo_consent(user["id"])
    if not consent["verified"] or not consent["authorized"]:
        return {
            "allowed": False,
            "reason": "ALGO_ORDER_CONSENT_REQUIRED",
            "message": "Automated-order acknowledgement is required before pairing",
            "subscription_status": status,
            "consent_verified": consent["verified"],
            "multi_user_enabled": True,
            "admin_only": False,
        }

    return {
        "allowed": True,
        "reason": "ELIGIBLE",
        "message": "This account can use its own static-IP gateway",
        "subscription_status": status,
        "consent_verified": True,
        "consent_accepted_at": consent["accepted_at"],
        "policy_version": consent["policy_version"],
        "multi_user_enabled": True,
        "admin_only": False,
    }


def require_gateway_user(user):
    access = gateway_access(user)
    if not access.get("allowed"):
        raise HTTPException(status_code=403, detail=access.get("message") or access.get("reason"))
    return access


def _authenticate_gateway(token):
    service.ensure_local_gateway_schema()
    token_hash = service._hash_secret(token)
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT g.*, u.email, u.is_admin, u.is_active,
                   u.subscription_status, u.trial_ends_at
            FROM local_gateways g
            JOIN users u ON u.id=g.user_id
            WHERE g.token_hash=? AND g.enabled=1
            LIMIT 1
            """,
            (token_hash,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not bool(row["is_active"]):
        raise HTTPException(status_code=401, detail="Invalid or disabled gateway token")
    return row


def apply_multi_user_gateway_patch():
    if getattr(service, "_okai_multi_user_gateway_v1", False):
        return

    original_heartbeat = service.heartbeat_gateway
    original_set_armed = service.set_gateway_armed
    original_get_status = service.get_gateway_status
    original_gateway_ready = service.gateway_ready

    def heartbeat_gateway(gateway, observed_ip, agent_version=""):
        status = original_heartbeat(gateway, observed_ip, agent_version)
        access = gateway_access(gateway["user_id"])
        if not access.get("allowed") and status.get("server_armed"):
            original_set_armed(gateway["user_id"], False)
            status["server_armed"] = False
        return {
            **status,
            "gateway_access_allowed": bool(access.get("allowed")),
            "gateway_access_reason": access.get("reason"),
            "gateway_access_message": access.get("message"),
            "multi_user_enabled": multi_user_enabled(),
            "policy_version": PATCH_VERSION,
        }

    def set_gateway_armed(user_id, armed):
        if armed:
            require_gateway_user(user_id)
        return original_set_armed(user_id, armed)

    def get_gateway_status(user_id):
        status = original_get_status(user_id)
        access = gateway_access(user_id)
        return {
            **status,
            "access": access,
            "admin_only": admin_only_enabled(),
            "multi_user_enabled": multi_user_enabled(),
            "policy_version": PATCH_VERSION,
        }

    def gateway_ready(user_id):
        access = gateway_access(user_id)
        if not access.get("allowed"):
            status = get_gateway_status(user_id)
            return False, access.get("reason") or "GATEWAY_ACCESS_DENIED", status
        return original_gateway_ready(user_id)

    service.admin_only_enabled = admin_only_enabled
    service.require_personal_user = require_gateway_user
    service.authenticate_gateway = _authenticate_gateway
    service.heartbeat_gateway = heartbeat_gateway
    service.set_gateway_armed = set_gateway_armed
    service.get_gateway_status = get_gateway_status
    service.gateway_ready = gateway_ready
    service.gateway_access = gateway_access
    service.multi_user_enabled = multi_user_enabled
    service._okai_multi_user_gateway_v1 = True
