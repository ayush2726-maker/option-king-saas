import copy
import json
import re
import uuid
from datetime import datetime

from database import get_db


DEFAULT_PROFILE_KEY = "okai_default_82"

INDICATOR_KEYS = (
    "vwap",
    "supertrend",
    "ema_trend",
    "orb",
    "momentum",
    "adx",
    "volume",
    "mtf",
)

DIRECTIONAL_KEYS = (
    "vwap",
    "supertrend",
    "ema_trend",
    "orb",
    "momentum",
)

DEFAULT_CONFIG = {
    "version": 1,
    "entry_threshold": 82,
    "weights": {
        "vwap": 11,
        "supertrend": 11,
        "ema_trend": 11,
        "orb": 11,
        "momentum": 11,
        "adx": 20,
        "volume": 15,
        "mtf": 10,
    },
    "enabled": {
        key: True
        for key in INDICATOR_KEYS
    },
    "adx_threshold": 25.0,
    "volume_threshold": 1.2,
    "sideways_mode": "cap",
    "anti_chase": {
        "ema_enabled": True,
        "vwap_enabled": True,
        "ema_min_points": 22.0,
        "vwap_min_points": 35.0,
        "ema_atr_multiplier": 1.2,
        "vwap_atr_multiplier": 2.0,
    },
    "paper_activation_only": True,
}


def _clamp(value, low, high, default):
    try:
        number = float(value)
    except Exception:
        return default

    return max(low, min(high, number))


def _bool(value, default=True):
    if value is None:
        return default

    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    return bool(value)


def _safe_name(value, default="Custom Strategy"):
    name = str(value or "").strip()
    name = re.sub(r"\s+", " ", name)
    return name[:48] or default


def _normalize_weights(weights, enabled):
    raw = {}

    for key in INDICATOR_KEYS:
        default = DEFAULT_CONFIG["weights"][key]
        raw[key] = int(
            round(
                _clamp(
                    (weights or {}).get(key, default),
                    0,
                    100,
                    default,
                )
            )
        )

        if not enabled.get(key, True):
            raw[key] = 0

    if not any(
        enabled.get(key, True)
        and raw.get(key, 0) > 0
        for key in DIRECTIONAL_KEYS
    ):
        enabled["vwap"] = True
        raw["vwap"] = max(
            1,
            DEFAULT_CONFIG["weights"]["vwap"],
        )

    total = sum(raw.values())

    if total <= 0:
        raw = copy.deepcopy(
            DEFAULT_CONFIG["weights"]
        )
        total = 100

    normalized = {
        key: int(
            round(raw[key] * 100 / total)
        )
        for key in INDICATOR_KEYS
    }

    diff = 100 - sum(normalized.values())

    receiver = next(
        (
            key
            for key in INDICATOR_KEYS
            if enabled.get(key, True)
        ),
        "vwap",
    )
    normalized[receiver] += diff

    return normalized


def normalize_profile_config(config=None):
    incoming = (
        copy.deepcopy(config)
        if isinstance(config, dict)
        else {}
    )
    result = copy.deepcopy(DEFAULT_CONFIG)

    enabled_in = incoming.get("enabled", {})
    enabled = {}

    for key in INDICATOR_KEYS:
        enabled[key] = _bool(
            enabled_in.get(
                key,
                result["enabled"][key],
            ),
            result["enabled"][key],
        )

    result["enabled"] = enabled
    result["weights"] = _normalize_weights(
        incoming.get("weights", {}),
        enabled,
    )

    result["entry_threshold"] = int(
        round(
            _clamp(
                incoming.get(
                    "entry_threshold",
                    result["entry_threshold"],
                ),
                50,
                99,
                result["entry_threshold"],
            )
        )
    )

    result["adx_threshold"] = round(
        _clamp(
            incoming.get(
                "adx_threshold",
                result["adx_threshold"],
            ),
            5,
            60,
            result["adx_threshold"],
        ),
        2,
    )

    result["volume_threshold"] = round(
        _clamp(
            incoming.get(
                "volume_threshold",
                result["volume_threshold"],
            ),
            0.1,
            5,
            result["volume_threshold"],
        ),
        2,
    )

    sideways_mode = str(
        incoming.get(
            "sideways_mode",
            result["sideways_mode"],
        )
    ).strip().lower()

    if sideways_mode not in {
        "block",
        "cap",
        "allow",
    }:
        sideways_mode = "cap"

    result["sideways_mode"] = sideways_mode

    anti_in = incoming.get(
        "anti_chase",
        {},
    )
    anti = result["anti_chase"]

    anti["ema_enabled"] = _bool(
        anti_in.get(
            "ema_enabled",
            anti["ema_enabled"],
        ),
        anti["ema_enabled"],
    )
    anti["vwap_enabled"] = _bool(
        anti_in.get(
            "vwap_enabled",
            anti["vwap_enabled"],
        ),
        anti["vwap_enabled"],
    )
    anti["ema_min_points"] = round(
        _clamp(
            anti_in.get(
                "ema_min_points",
                anti["ema_min_points"],
            ),
            0,
            500,
            anti["ema_min_points"],
        ),
        2,
    )
    anti["vwap_min_points"] = round(
        _clamp(
            anti_in.get(
                "vwap_min_points",
                anti["vwap_min_points"],
            ),
            0,
            1000,
            anti["vwap_min_points"],
        ),
        2,
    )
    anti["ema_atr_multiplier"] = round(
        _clamp(
            anti_in.get(
                "ema_atr_multiplier",
                anti["ema_atr_multiplier"],
            ),
            0,
            10,
            anti["ema_atr_multiplier"],
        ),
        2,
    )
    anti["vwap_atr_multiplier"] = round(
        _clamp(
            anti_in.get(
                "vwap_atr_multiplier",
                anti["vwap_atr_multiplier"],
            ),
            0,
            15,
            anti["vwap_atr_multiplier"],
        ),
        2,
    )

    result["anti_chase"] = anti
    result["paper_activation_only"] = True
    result["version"] = 1

    return result


def ensure_profile_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            profile_key TEXT NOT NULL,
            name TEXT NOT NULL,
            config_json TEXT NOT NULL,
            locked INTEGER DEFAULT 0,
            active INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(user_id, profile_key)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_strategy_profiles_user_active
        ON strategy_profiles(user_id, active)
        """
    )
    conn.commit()


def _row_to_profile(row):
    try:
        config = normalize_profile_config(
            json.loads(
                row["config_json"] or "{}"
            )
        )
    except Exception:
        config = normalize_profile_config()

    return {
        "profile_key": row["profile_key"],
        "name": row["name"],
        "locked": bool(row["locked"]),
        "active": bool(row["active"]),
        "config": config,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _ensure_default_profile(conn, user_id):
    ensure_profile_tables(conn)
    now = datetime.utcnow().isoformat()
    default_json = json.dumps(
        normalize_profile_config(),
        separators=(",", ":"),
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO strategy_profiles (
            user_id,
            profile_key,
            name,
            config_json,
            locked,
            active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, 1, 1, ?, ?)
        """,
        (
            user_id,
            DEFAULT_PROFILE_KEY,
            "OKAI Default 82",
            default_json,
            now,
            now,
        ),
    )

    conn.execute(
        """
        UPDATE strategy_profiles
        SET name=?,
            config_json=?,
            locked=1,
            updated_at=?
        WHERE user_id=?
          AND profile_key=?
        """,
        (
            "OKAI Default 82",
            default_json,
            now,
            user_id,
            DEFAULT_PROFILE_KEY,
        ),
    )

    active = conn.execute(
        """
        SELECT profile_key
        FROM strategy_profiles
        WHERE user_id=?
          AND active=1
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()

    if not active:
        conn.execute(
            """
            UPDATE strategy_profiles
            SET active=CASE
                WHEN profile_key=? THEN 1
                ELSE 0
            END
            WHERE user_id=?
            """,
            (
                DEFAULT_PROFILE_KEY,
                user_id,
            ),
        )

    conn.commit()


def list_strategy_profiles(user_id):
    conn = get_db()

    try:
        _ensure_default_profile(
            conn,
            user_id,
        )
        rows = conn.execute(
            """
            SELECT *
            FROM strategy_profiles
            WHERE user_id=?
            ORDER BY active DESC,
                     locked DESC,
                     updated_at DESC,
                     id DESC
            """,
            (user_id,),
        ).fetchall()

        return [
            _row_to_profile(row)
            for row in rows
        ]
    finally:
        conn.close()


def get_active_profile_config(user_id):
    conn = get_db()

    try:
        _ensure_default_profile(
            conn,
            user_id,
        )
        row = conn.execute(
            """
            SELECT *
            FROM strategy_profiles
            WHERE user_id=?
              AND active=1
            ORDER BY locked ASC, id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()

        if not row:
            row = conn.execute(
                """
                SELECT *
                FROM strategy_profiles
                WHERE user_id=?
                  AND profile_key=?
                LIMIT 1
                """,
                (
                    user_id,
                    DEFAULT_PROFILE_KEY,
                ),
            ).fetchone()

        profile = _row_to_profile(row)
        flattened = copy.deepcopy(
            profile["config"]
        )
        flattened.update({
            "profile_key": profile[
                "profile_key"
            ],
            "profile_name": profile[
                "name"
            ],
            "profile_locked": profile[
                "locked"
            ],
            "profile_active": True,
        })

        return flattened
    finally:
        conn.close()


def create_strategy_profile(
    user_id,
    name,
    config=None,
):
    conn = get_db()

    try:
        _ensure_default_profile(
            conn,
            user_id,
        )
        count = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM strategy_profiles
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()["total"]

        if int(count or 0) >= 12:
            raise ValueError(
                "Maximum 12 strategy profiles allowed"
            )

        key = (
            "custom_"
            + uuid.uuid4().hex[:12]
        )
        clean_config = (
            normalize_profile_config(
                config
            )
        )
        clean_name = _safe_name(name)
        now = datetime.utcnow().isoformat()

        conn.execute(
            """
            INSERT INTO strategy_profiles (
                user_id,
                profile_key,
                name,
                config_json,
                locked,
                active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                user_id,
                key,
                clean_name,
                json.dumps(
                    clean_config,
                    separators=(",", ":"),
                ),
                now,
                now,
            ),
        )
        conn.commit()

        row = conn.execute(
            """
            SELECT *
            FROM strategy_profiles
            WHERE user_id=?
              AND profile_key=?
            """,
            (
                user_id,
                key,
            ),
        ).fetchone()

        return _row_to_profile(row)
    finally:
        conn.close()


def update_strategy_profile(
    user_id,
    profile_key,
    name,
    config,
):
    conn = get_db()

    try:
        _ensure_default_profile(
            conn,
            user_id,
        )
        row = conn.execute(
            """
            SELECT *
            FROM strategy_profiles
            WHERE user_id=?
              AND profile_key=?
            """,
            (
                user_id,
                profile_key,
            ),
        ).fetchone()

        if not row:
            raise ValueError(
                "Strategy profile not found"
            )

        if bool(row["locked"]):
            raise ValueError(
                "OKAI Default 82 is locked"
            )

        clean_name = _safe_name(
            name,
            row["name"],
        )
        clean_config = (
            normalize_profile_config(
                config
            )
        )

        conn.execute(
            """
            UPDATE strategy_profiles
            SET name=?,
                config_json=?,
                updated_at=?
            WHERE user_id=?
              AND profile_key=?
            """,
            (
                clean_name,
                json.dumps(
                    clean_config,
                    separators=(",", ":"),
                ),
                datetime.utcnow().isoformat(),
                user_id,
                profile_key,
            ),
        )
        conn.commit()

        updated = conn.execute(
            """
            SELECT *
            FROM strategy_profiles
            WHERE user_id=?
              AND profile_key=?
            """,
            (
                user_id,
                profile_key,
            ),
        ).fetchone()

        return _row_to_profile(
            updated
        )
    finally:
        conn.close()


def duplicate_strategy_profile(
    user_id,
    profile_key,
    name=None,
):
    profiles = list_strategy_profiles(
        user_id
    )
    source = next(
        (
            profile
            for profile in profiles
            if profile["profile_key"]
            == profile_key
        ),
        None,
    )

    if not source:
        raise ValueError(
            "Strategy profile not found"
        )

    duplicate_name = _safe_name(
        name,
        f"{source['name']} Copy",
    )

    return create_strategy_profile(
        user_id,
        duplicate_name,
        source["config"],
    )


def activate_strategy_profile(
    user_id,
    profile_key,
):
    conn = get_db()

    try:
        _ensure_default_profile(
            conn,
            user_id,
        )
        row = conn.execute(
            """
            SELECT *
            FROM strategy_profiles
            WHERE user_id=?
              AND profile_key=?
            """,
            (
                user_id,
                profile_key,
            ),
        ).fetchone()

        if not row:
            raise ValueError(
                "Strategy profile not found"
            )

        conn.execute(
            """
            UPDATE strategy_profiles
            SET active=CASE
                WHEN profile_key=? THEN 1
                ELSE 0
            END,
            updated_at=CASE
                WHEN profile_key=? THEN ?
                ELSE updated_at
            END
            WHERE user_id=?
            """,
            (
                profile_key,
                profile_key,
                datetime.utcnow().isoformat(),
                user_id,
            ),
        )
        conn.commit()

        active = conn.execute(
            """
            SELECT *
            FROM strategy_profiles
            WHERE user_id=?
              AND profile_key=?
            """,
            (
                user_id,
                profile_key,
            ),
        ).fetchone()

        return _row_to_profile(
            active
        )
    finally:
        conn.close()


def delete_strategy_profile(
    user_id,
    profile_key,
):
    conn = get_db()

    try:
        _ensure_default_profile(
            conn,
            user_id,
        )
        row = conn.execute(
            """
            SELECT *
            FROM strategy_profiles
            WHERE user_id=?
              AND profile_key=?
            """,
            (
                user_id,
                profile_key,
            ),
        ).fetchone()

        if not row:
            raise ValueError(
                "Strategy profile not found"
            )

        if bool(row["locked"]):
            raise ValueError(
                "OKAI Default 82 cannot be deleted"
            )

        if bool(row["active"]):
            raise ValueError(
                "Activate another profile before deleting"
            )

        conn.execute(
            """
            DELETE FROM strategy_profiles
            WHERE user_id=?
              AND profile_key=?
            """,
            (
                user_id,
                profile_key,
            ),
        )
        conn.commit()

        return True
    finally:
        conn.close()
