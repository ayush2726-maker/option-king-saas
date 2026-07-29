"""Force Live Strategy Score display to use the active strategy snapshot.

The trading engine already receives the active profile before every AUTO scan.
This patch makes that same profile explicit on each scan payload and removes any
stale precomputed live_score_breakdown so the display wrapper recomputes the
visible per-indicator score using the latest active weights and thresholds.

Display-only: it does not change entry, exit, order placement, quantity, SL, or
risk decisions.
"""

from __future__ import annotations

from bot import auto_portfolio_runtime as runtime


DEFAULT_PROFILE_KEY = "okai_default_82"
DEFAULT_PROFILE_NAME = "OKAI Default 82"


def _i(value, default=0):
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _f(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _dict(value):
    return dict(value) if isinstance(value, dict) else {}


def _profile_snapshot(profile, signal):
    profile = _dict(profile)
    signal = _dict(signal)

    weights = _dict(profile.get("weights")) or _dict(signal.get("profile_weights"))
    enabled = _dict(profile.get("enabled")) or _dict(signal.get("profile_enabled"))

    return {
        "profile_key": str(
            profile.get("profile_key")
            or signal.get("strategy_profile_key")
            or DEFAULT_PROFILE_KEY
        ),
        "profile_name": str(
            profile.get("profile_name")
            or signal.get("strategy_profile_name")
            or DEFAULT_PROFILE_NAME
        ),
        "weights": weights,
        "enabled": enabled,
        "entry_threshold": _i(
            profile.get(
                "entry_threshold",
                signal.get("min_score", signal.get("min_score_required", 82)),
            ),
            82,
        ),
        "adx_threshold": _f(
            profile.get("adx_threshold", signal.get("adx_threshold", 25.0)),
            25.0,
        ),
        "volume_threshold": _f(
            profile.get("volume_threshold", signal.get("volume_threshold", 1.2)),
            1.2,
        ),
    }


def _apply_snapshot_to_signal(signal, snapshot):
    if not isinstance(signal, dict):
        return

    signal["strategy_profile_key"] = snapshot["profile_key"]
    signal["strategy_profile_name"] = snapshot["profile_name"]
    signal["profile_weights"] = dict(snapshot["weights"])
    signal["profile_enabled"] = dict(snapshot["enabled"])
    signal["adx_threshold"] = snapshot["adx_threshold"]
    signal["volume_threshold"] = snapshot["volume_threshold"]
    signal["min_score"] = snapshot["entry_threshold"]
    signal["min_score_required"] = snapshot["entry_threshold"]

    # Older scans may already carry a breakdown that was calculated before the
    # current editable profile was attached. Removing it forces the display-only
    # live score wrapper to recompute with the active weights and thresholds.
    signal.pop("live_score_breakdown", None)
    signal.pop("score_components", None)


def apply_active_strategy_score_patch() -> None:
    if getattr(runtime, "_okai_active_strategy_score_v1", False):
        return

    original_build_scan = runtime._build_scan

    def build_scan_with_active_profile(user_id, underlying, df, profile, loss_streak):
        scan = original_build_scan(user_id, underlying, df, profile, loss_streak)

        try:
            if not isinstance(scan, dict):
                return scan

            signal = scan.get("signal_data")
            if not isinstance(signal, dict):
                return scan

            snapshot = _profile_snapshot(profile, signal)
            _apply_snapshot_to_signal(signal, snapshot)

            scan["profile_config"] = {
                "profile_key": snapshot["profile_key"],
                "profile_name": snapshot["profile_name"],
                "weights": dict(snapshot["weights"]),
                "enabled": dict(snapshot["enabled"]),
                "entry_threshold": snapshot["entry_threshold"],
                "adx_threshold": snapshot["adx_threshold"],
                "volume_threshold": snapshot["volume_threshold"],
            }
        except Exception as exc:
            try:
                print(f"Active strategy score snapshot skipped: {str(exc)[:160]}")
            except Exception:
                pass

        return scan

    runtime._build_scan = build_scan_with_active_profile
    runtime._okai_active_strategy_score_v1 = True
