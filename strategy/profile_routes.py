from fastapi import (
    APIRouter,
    Header,
    HTTPException,
)

from auth.routes import get_current_user
from bot.default_strategy_patch import editable_template_config
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


def _is_admin(user):
    try:
        return bool(user.get("is_admin"))
    except Exception:
        try:
            return bool(user["is_admin"])
        except Exception:
            return False


def _ensure_admin_editable_active(user, profiles):
    """Ensure owner has an editable copy without changing activation.

    Default must remain the locked original strategy. The editable copy is
    created from its own custom template, not by duplicating Default. Activation
    always remains the user's explicit choice.
    """
    if not _is_admin(user):
        return profiles, False

    editable = next(
        (
            profile
            for profile in profiles
            if not profile.get("locked")
            and str(profile.get("name") or "").startswith("OKAI Editable")
        ),
        None,
    )
    if editable is not None:
        return profiles, False

    create_strategy_profile(
        user["id"],
        "OKAI Editable 82",
        editable_template_config(),
    )
    return list_strategy_profiles(user["id"]), True


def _refresh_running_engine(user_id):
    """Drop only the stale in-memory engine after an active-profile change.

    The persisted bot-running flag is intentionally left untouched. The next
    /bot/signal poll recreates the broker engine and its first scan reads the
    freshly saved active profile. Open positions remain in the database and are
    therefore not duplicated or closed by this refresh.
    """
    result = {
        "requested": False,
        "was_running": False,
        "message": "Bot was not running; strategy will apply on next start.",
    }

    try:
        from bot.angel_fetcher import get_user_bot_state, stop_user_bot

        state = get_user_bot_state(user_id) or {}
        result["was_running"] = bool(state.get("running"))
        if not result["was_running"]:
            return result

        stopped = stop_user_bot(user_id) or {}
        result["requested"] = bool(stopped.get("success"))
        result["message"] = (
            "Running engine refresh requested; next live-score poll will start "
            "with the saved active strategy."
            if result["requested"]
            else str(stopped.get("message") or "Engine refresh could not be requested")
        )
    except Exception as exc:
        result["message"] = f"Engine refresh warning: {str(exc)[:160]}"

    return result


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
    editable_created = False

    try:
        profiles, editable_created = _ensure_admin_editable_active(
            user,
            profiles,
        )
    except Exception:
        # Profile listing must remain available even if the one-time editable
        # migration cannot run. The protected default remains safe.
        editable_created = False

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
        "admin_editable_ready": bool(
            _is_admin(user)
            and any(not profile.get("locked") for profile in profiles)
        ),
        "editable_profile_created": editable_created,
        "version": 4,
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

    runtime_refresh = (
        _refresh_running_engine(user["id"])
        if profile.get("active")
        else {
            "requested": False,
            "was_running": False,
            "message": "Saved profile is not active; running engine unchanged.",
        }
    )

    return {
        "success": True,
        "message": "Strategy profile saved",
        "profile": profile,
        "runtime_refresh": runtime_refresh,
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

    active_config = get_active_profile_config(
        user["id"]
    )
    runtime_refresh = _refresh_running_engine(
        user["id"]
    )

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
        "active_config": active_config,
        "runtime_refresh": runtime_refresh,
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
