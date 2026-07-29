"""Keep OKAI Default 82 protected and separate from editable profiles.

OKAI Default 82 must stay on the original protected TQU logic.  The admin's
OKAI Editable 82 is a separate custom-profile template that can be tuned without
changing the locked default.  Startup migrations may refresh only the locked
default row; user-editable/custom profiles are never overwritten during deploy.
"""

import copy
import json
from datetime import datetime

from bot import strategy
from database import get_db
from strategy import profile_engine


# Original locked default / protected profile shape.  These numbers only describe
# the Strategy Builder display/profile row for the default.  Actual Default trade
# decisions are still made by bot.strategy.get_full_signal's protected TQU path.
LEGACY_DEFAULT_WEIGHTS = {
    "vwap": 11,
    "supertrend": 11,
    "ema_trend": 11,
    "orb": 11,
    "momentum": 11,
    "adx": 20,
    "volume": 15,
    "mtf": 10,
}
LEGACY_DEFAULT_ADX_THRESHOLD = 25.0

# Backward-compatible names used by older display patches as default fallbacks.
TARGET_WEIGHTS = LEGACY_DEFAULT_WEIGHTS
TARGET_ADX_THRESHOLD = LEGACY_DEFAULT_ADX_THRESHOLD

# Editable template remains separate.  Existing unlocked profiles are preserved;
# these values are used only when the admin editable profile is missing and has to
# be created for the first time.
EDITABLE_TEMPLATE_WEIGHTS = {
    "vwap": 15,
    "supertrend": 18,
    "ema_trend": 28,
    "orb": 0,
    "momentum": 10,
    "adx": 14,
    "volume": 5,
    "mtf": 10,
}
EDITABLE_TEMPLATE_ADX_THRESHOLD = 22.0


def _profile_config(weights, adx_threshold, version=1, base=None):
    config = copy.deepcopy(
        base if isinstance(base, dict) else profile_engine.DEFAULT_CONFIG
    )
    config["entry_threshold"] = 82
    config["weights"] = copy.deepcopy(weights)
    config["adx_threshold"] = float(adx_threshold)
    config["volume_threshold"] = 1.2
    config["enabled"] = {key: True for key in profile_engine.INDICATOR_KEYS}
    config["version"] = version
    return profile_engine.normalize_profile_config(config)


def default_profile_config(base=None):
    return _profile_config(
        LEGACY_DEFAULT_WEIGHTS,
        LEGACY_DEFAULT_ADX_THRESHOLD,
        version=1,
        base=base,
    )


def editable_template_config(base=None):
    return _profile_config(
        EDITABLE_TEMPLATE_WEIGHTS,
        EDITABLE_TEMPLATE_ADX_THRESHOLD,
        version=3,
        base=base,
    )


def _default_profile(profile=None):
    config = default_profile_config(profile)
    config.update({
        "profile_key": profile_engine.DEFAULT_PROFILE_KEY,
        "profile_name": "OKAI Default 82",
        "profile_locked": True,
        "profile_active": True,
    })
    return config


def apply_default_strategy_patch():
    """Keep Default on original TQU; custom profiles stay on builder logic."""
    if getattr(strategy, "_okai_default_independent_v1", False):
        return

    # Do not mutate profile_engine.DEFAULT_CONFIG and do not wrap get_full_signal.
    # bot.strategy.get_full_signal already sends okai_default_82 through the
    # protected TQU logic, while non-default profile keys use CUSTOM_PROFILE_V1.
    strategy._okai_default_independent_v1 = True


def migrate_default_strategy_profiles():
    """Update only locked defaults; preserve editable/custom user changes."""
    conn = get_db()
    try:
        profile_engine.ensure_profile_tables(conn)
        now = datetime.utcnow().isoformat()
        default_json = json.dumps(
            default_profile_config(),
            separators=(",", ":"),
        )

        # Keep only the protected OKAI default in sync with the original logic.
        # Do not touch unlocked profiles, including the admin's OKAI Editable 82
        # copy.  Editable/custom profiles must remain separate from Default.
        conn.execute(
            """
            UPDATE strategy_profiles
            SET name='OKAI Default 82',
                config_json=?,
                locked=1,
                updated_at=?
            WHERE profile_key=?
              AND COALESCE(locked, 0)=1
            """,
            (
                default_json,
                now,
                profile_engine.DEFAULT_PROFILE_KEY,
            ),
        )
        conn.commit()
    finally:
        conn.close()
