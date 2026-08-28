from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth.utils import decode_token
from alexa_option_king import routes as legacy

router = APIRouter(tags=["Alexa Option King"])
_current_user_id: ContextVar[int | None] = ContextVar("alexa_option_king_user_id", default=None)
_legacy_owner_user_id = legacy._owner_user_id


def _account_linking_required() -> bool:
    return str(os.getenv("ALEXA_REQUIRE_ACCOUNT_LINKING", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _linked_user_id(payload: dict[str, Any]) -> int | None:
    context_user = ((((payload.get("context") or {}).get("System") or {}).get("user") or {}))
    session_user = (((payload.get("session") or {}).get("user") or {}))
    token = str(context_user.get("accessToken") or session_user.get("accessToken") or "").strip()
    if not token:
        return None
    data = decode_token(token)
    user_id = data.get("user_id")
    if user_id is None:
        raise ValueError("Linked account token has no user id")
    return int(user_id)


def _multiuser_owner_user_id() -> int:
    linked = _current_user_id.get()
    if linked is not None:
        return int(linked)
    if _account_linking_required():
        raise RuntimeError("Alexa account linking is required")
    # Backward-compatible fallback for the Alexa simulator until Account Linking
    # is enabled in the Developer Console. Once enabled, set
    # ALEXA_REQUIRE_ACCOUNT_LINKING=1 in Railway so unlinked users never see data.
    return _legacy_owner_user_id()


legacy._owner_user_id = _multiuser_owner_user_id


def _link_required_response() -> dict[str, Any]:
    response = legacy._alexa_response(
        "Option King use karne ke liye Alexa app mein apna Option King account link karo.",
        end_session=True,
    )
    response["response"]["card"] = {
        "type": "LinkAccount",
    }
    return response


@router.post("/api/alexa")
async def alexa_endpoint(request: Request):
    try:
        payload = await request.json()
        try:
            user_id = _linked_user_id(payload)
        except Exception as exc:
            print(f"OPTION KING ALEXA LINK TOKEN ERROR | {exc}", flush=True)
            return JSONResponse(status_code=200, content=_link_required_response())

        if user_id is None and _account_linking_required():
            return JSONResponse(status_code=200, content=_link_required_response())

        marker = _current_user_id.set(user_id)
        try:
            result = legacy._dispatch(payload)
        finally:
            _current_user_id.reset(marker)
        return JSONResponse(status_code=200, content=result)
    except Exception as exc:
        print(f"OPTION KING ALEXA REQUEST ERROR | {exc}", flush=True)
        return JSONResponse(status_code=400, content={"error": "invalid alexa request"})


@router.get("/api/alexa/health")
def alexa_health():
    return {
        "status": "ok",
        "mode": "multi_user_read_only",
        "skill_id": legacy.ALEXA_SKILL_ID,
        "account_linking_required": _account_linking_required(),
    }
