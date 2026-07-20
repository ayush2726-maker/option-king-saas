from fastapi import (
    APIRouter,
    Header,
    HTTPException,
)

from auth.routes import get_current_user
from telegram.routes import notify_user
from strategy.profile_engine import (
    activate_strategy_profile,
    create_strategy_profile,
    delete_strategy_profile,
    duplicate_strategy_profile,
    get_active_profile_config,
    list_strategy_profiles,
    update_strategy_profile,
)


router = APIRouter(
    prefix="/strategy/profiles",
    tags=["Strategy Profiles"],
)


def _error(exc):
    raise HTTPException(
        status_code=400,
        detail=str(exc),
    )


@router.get("")
def get_profiles(
    authorization: str = Header(None),
):
    user = get_current_user(
        authorization
    )
    profiles = list_strategy_profiles(
        user["id"]
    )
    active = next(
        (
            profile
            for profile in profiles
            if profile["active"]
        ),
        None,
    )

    return {
        "success": True,
        "profiles": profiles,
        "active_profile": active,
        "activation_mode": "paper",
        "live_activation_available": False,
        "version": 1,
    }


@router.post("")
def create_profile(
    body: dict,
    authorization: str = Header(None),
):
    user = get_current_user(
        authorization
    )

    try:
        profile = create_strategy_profile(
            user["id"],
            (body or {}).get(
                "name",
                "Custom Strategy",
            ),
            (body or {}).get(
                "config",
                {},
            ),
        )
    except Exception as exc:
        _error(exc)

    return {
        "success": True,
        "message": "Strategy profile created",
        "profile": profile,
    }


@router.post("/{profile_key}")
def update_profile(
    profile_key: str,
    body: dict,
    authorization: str = Header(None),
):
    user = get_current_user(
        authorization
    )

    try:
        profile = update_strategy_profile(
            user["id"],
            profile_key,
            (body or {}).get("name"),
            (body or {}).get(
                "config",
                {},
            ),
        )
    except Exception as exc:
        _error(exc)

    return {
        "success": True,
        "message": "Strategy profile saved",
        "profile": profile,
    }


@router.post(
    "/{profile_key}/duplicate"
)
def duplicate_profile(
    profile_key: str,
    body: dict,
    authorization: str = Header(None),
):
    user = get_current_user(
        authorization
    )

    try:
        profile = duplicate_strategy_profile(
            user["id"],
            profile_key,
            (body or {}).get("name"),
        )
    except Exception as exc:
        _error(exc)

    return {
        "success": True,
        "message": "Strategy profile duplicated",
        "profile": profile,
    }


@router.post(
    "/{profile_key}/activate"
)
def activate_profile(
    profile_key: str,
    body: dict = None,
    authorization: str = Header(None),
):
    user = get_current_user(
        authorization
    )

    requested_mode = str(
        (body or {}).get(
            "mode",
            "paper",
        )
    ).lower()

    if requested_mode != "paper":
        raise HTTPException(
            status_code=400,
            detail=(
                "Strategy Builder V1 activation "
                "is paper mode only"
            ),
        )

    try:
        profile = activate_strategy_profile(
            user["id"],
            profile_key,
        )
    except Exception as exc:
        _error(exc)

    notify_user(
        user["id"],
        (
            "🧠 <b>Strategy Activated</b>\n"
            f"Profile: {profile['name']}\n"
            "Mode: PAPER\n"
            f"Entry Score: "
            f"{profile['config']['entry_threshold']}"
        ),
    )

    return {
        "success": True,
        "message": (
            "Strategy activated for Paper Mode"
        ),
        "profile": profile,
        "active_config": (
            get_active_profile_config(
                user["id"]
            )
        ),
    }


@router.delete("/{profile_key}")
def delete_profile(
    profile_key: str,
    authorization: str = Header(None),
):
    user = get_current_user(
        authorization
    )

    try:
        delete_strategy_profile(
            user["id"],
            profile_key,
        )
    except Exception as exc:
        _error(exc)

    return {
        "success": True,
        "message": "Strategy profile deleted",
    }
