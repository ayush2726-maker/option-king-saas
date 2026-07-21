"""Make the balanced fresh-breakout profile the permanent OKAI Default 82.

The protected threshold remains 82.  This module changes only the score mix and
ensures the locked default uses the same weighted profile engine as the editable
copy.  Existing unrelated custom profiles are not overwritten.
"""

import copy
import json
from datetime import datetime

from bot import strategy
from bot import angel_fetcher
from bot import routes
from database import get_db
from strategy import profile_engine


TARGET_WEIGHTS = {
    "vwap": 15,
    "supertrend": 13,
    "ema_trend": 18,
    "orb": 15,
    "momentum": 10,
    "adx": 14,
    "volume": 5,
    "mtf": 10,
}


def _target_config(base=None):
    config = copy.deepcopy(
        base if isinstance(base, dict) else profile_engine.DEFAULT_CONFIG
    )
    config["entry_threshold"] = 82
    config["weights"] = copy.deepcopy(TARGET_WEIGHTS)
    config["enabled"] = {
        key: True
        for key in profile_engine.INDICATOR_KEYS
    }
    config["version"] = 2
    return profile_engine.normalize_profile_config(config)


def _default_profile(profile=None):
    config = _target_config(profile)
    config.update({
        "profile_key": profile_engine.DEFAULT_PROFILE_KEY,
        "profile_name": "OKAI Default 82",
        "profile_locked": True,
        "profile_active": True,
    })
    return config


def apply_default_strategy_patch():
    """Apply once before Fresh Entry Guard wraps the signal function."""
    if getattr(strategy, "_okai_balanced_default_v1", False):
        return

    profile_engine.DEFAULT_CONFIG["weights"] = copy.deepcopy(TARGET_WEIGHTS)
    profile_engine.DEFAULT_CONFIG["entry_threshold"] = 82
    profile_engine.DEFAULT_CONFIG["version"] = 2

    original_get_full_signal = strategy.get_full_signal

    def balanced_get_full_signal(
        market_data,
        consecutive_losses=0,
        profile=None,
    ):
        profile_key = str(
            (profile or {}).get(
                "profile_key",
                profile_engine.DEFAULT_PROFILE_KEY,
            )
        )

        if profile is None or profile_key == profile_engine.DEFAULT_PROFILE_KEY:
            return strategy._custom_profile_signal(
                market_data,
                _default_profile(profile),
                consecutive_losses,
            )

        return original_get_full_signal(
            market_data,
            consecutive_losses=consecutive_losses,
            profile=profile,
        )

    strategy.get_full_signal = balanced_get_full_signal
    routes.get_full_signal = balanced_get_full_signal
    angel_fetcher.get_full_signal = balanced_get_full_signal
    strategy._okai_balanced_default_v1 = True


def migrate_default_strategy_profiles():
    """Update locked defaults and the owner's generated editable default copy."""
    conn = get_db()
    try:
        profile_engine.ensure_profile_tables(conn)
        now = datetime.utcnow().isoformat()
        default_json = json.dumps(
            _target_config(),
            separators=(",", ":"),
        )

        # All locked defaults become the new permanent default.
        conn.execute(
            """
            UPDATE strategy_profiles
            SET name='OKAI Default 82',
                config_json=?,
                locked=1,
                updated_at=?
            WHERE profile_key=?
            """,
            (
                default_json,
                now,
                profile_engine.DEFAULT_PROFILE_KEY,
            ),
        )

        # The admin's automatically generated editable copy should match the
        # new default immediately.  Other user-created custom profiles remain
        # untouched.
        conn.execute(
            """
            UPDATE strategy_profiles
            SET config_json=?,
                updated_at=?
            WHERE locked=0
              AND name LIKE 'OKAI Editable%'
              AND user_id IN (
                  SELECT id FROM users
                  WHERE COALESCE(is_admin, 0)=1
              )
            """,
            (default_json, now),
        )
        conn.commit()
    finally:
        conn.close()
