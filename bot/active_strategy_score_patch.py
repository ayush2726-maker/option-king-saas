"""Force Live Strategy Score display to use the active strategy snapshot.

The trading engine already receives the active profile before every AUTO scan.
This patch makes that same profile explicit on each scan payload and removes any
stale precomputed live_score_breakdown so the display wrapper recomputes the
visible per-indicator score using the latest active weights and thresholds.

Display scoring is display-only: it does not change entry, exit, order
placement, quantity, SL, or risk decisions.  The small LIVE exit duplicate guard
below only prevents a second SELL from being placed while a previous LIVE exit
order is already pending for the same open trade.
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
            profile.get("adx_threshold", signal.get("adx_threshold", 22.0)),
            22.0,
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
    # current active profile was attached. Removing it prevents stale display
    # data like ADX threshold 25.0 while the active strategy is ADX 22.0.
    signal.pop("live_score_breakdown", None)
    signal.pop("score_components", None)


def _profile_like(snapshot):
    return {
        "weights": dict(snapshot["weights"]),
        "enabled": dict(snapshot["enabled"]),
        "entry_threshold": snapshot["entry_threshold"],
        "adx_threshold": snapshot["adx_threshold"],
        "volume_threshold": snapshot["volume_threshold"],
        "profile_key": snapshot["profile_key"],
        "profile_name": snapshot["profile_name"],
    }


def _display_signal(signal, snapshot):
    """Copy the signal and force active-profile values to win for display."""
    display_signal = dict(signal or {})
    _apply_snapshot_to_signal(display_signal, snapshot)

    # Some older wrappers write engine defaults (for example ADX 25) directly on
    # the signal before the active profile is attached.  The score breakdown uses
    # result.* values before profile.* values, so make the display copy explicit.
    display_signal["adx_threshold"] = snapshot["adx_threshold"]
    display_signal["volume_threshold"] = snapshot["volume_threshold"]
    display_signal["min_score"] = snapshot["entry_threshold"]
    display_signal["min_score_required"] = snapshot["entry_threshold"]
    display_signal["profile_weights"] = dict(snapshot["weights"])
    display_signal["profile_enabled"] = dict(snapshot["enabled"])
    return display_signal


def _recompute_display_payload(scan, signal, snapshot):
    """Rebuild display-only score rows after the active profile is attached."""
    if not isinstance(scan, dict) or not isinstance(signal, dict):
        return

    try:
        from bot.live_score_breakdown_patch import _score_payload

        display_signal = _display_signal(signal, snapshot)
        payload = _score_payload(
            scan.get("market_data") or {},
            display_signal,
            _profile_like(snapshot),
        )
        signal["score_components"] = payload.get("components", [])
        signal["live_score_breakdown"] = payload
        signal["display_score"] = payload.get("display_score", payload.get("score"))
        signal["decision_score"] = signal.get("score")
        signal["component_total"] = payload.get("component_total")
        signal["decision_component_total"] = payload.get("decision_component_total")
        signal["enabled_weight_total"] = payload.get("enabled_weight_total")
        scan["score_components"] = payload.get("components", [])
        scan["live_score_breakdown"] = payload
        scan["display_score"] = payload.get("display_score", payload.get("score"))
        scan["decision_score"] = signal.get("score")
    except Exception as exc:
        try:
            print(f"Active strategy display recompute skipped: {str(exc)[:160]}")
        except Exception:
            pass


def _install_trade_miss_audit():
    try:
        from bot.trade_miss_audit_patch import apply_trade_miss_audit_patch

        apply_trade_miss_audit_patch()
    except Exception as exc:
        try:
            print(f"Trade miss audit patch skipped: {str(exc)[:160]}")
        except Exception:
            pass


def _install_live_exit_duplicate_guard():
    """Prevent duplicate LIVE SELL orders while an exit is already pending.

    The original runtime already records EXIT_PENDING and sets live_order_lock
    when a broker SELL is not confirmed within the wait window.  This guard makes
    the next monitor ticks skip that same open row instead of placing another
    SELL for the same trade. PAPER exits are untouched.
    """
    if getattr(runtime, "_okai_live_exit_duplicate_guard_v1", False):
        return

    original_manage_rows = runtime._manage_rows

    def manage_rows_with_live_exit_duplicate_guard(
        conn,
        user_id,
        rows,
        scans,
        quote_fetcher,
        live_order,
        state,
    ):
        safe_rows = []
        blocked = []

        for row in rows or []:
            try:
                is_live = runtime._mode(row) == "live"
                exit_order_id = str(runtime._v(row, "exit_order_id", "") or "").strip()
                exit_status = str(
                    runtime._v(row, "live_order_status", "") or ""
                ).upper().strip()
                pending_exit = exit_status == "EXIT_PENDING" or (
                    bool(exit_order_id)
                    and exit_status not in {"EXIT_FAILED", "EXIT_REJECTED", "EXIT_CANCELLED", "EXIT_CANCELED"}
                )
                if is_live and pending_exit:
                    blocked.append(
                        {
                            "trade_id": runtime._v(row, "id", None),
                            "exit_order_id": exit_order_id,
                            "live_order_status": exit_status or "EXIT_PENDING",
                        }
                    )
                    continue
            except Exception:
                pass

            safe_rows.append(row)

        if blocked and isinstance(state, dict):
            state["live_order_lock"] = True
            state["live_exit_duplicate_guard"] = {
                "blocked_rows": len(blocked),
                "message": "Existing LIVE exit order pending; duplicate SELL blocked.",
                "rows": blocked[:5],
            }
            state["live_order_error"] = "Existing LIVE exit order pending; duplicate SELL blocked."

        return original_manage_rows(
            conn,
            user_id,
            safe_rows,
            scans,
            quote_fetcher,
            live_order,
            state,
        )

    runtime._manage_rows = manage_rows_with_live_exit_duplicate_guard
    runtime._okai_live_exit_duplicate_guard_v1 = True


def _sync_state_display_score(state, scans, selected, settings):
    """Keep /bot/signal aligned with the active-profile breakdown display.

    Entry selection has already completed before _state_update runs, so this is
    display-only.  It prevents the app header/card from showing the old engine
    score while the breakdown rows show the active-profile partial score.
    """
    try:
        display = runtime._display_scan(scans, selected, settings)
        if not isinstance(display, dict):
            return
        signal = display.get("signal_data") or {}
        payload = signal.get("live_score_breakdown") or display.get("live_score_breakdown")
        if not isinstance(payload, dict):
            return

        display_score = _i(payload.get("display_score", payload.get("score", state.get("score", 0))))
        state["score"] = display_score
        state["display_score"] = display_score
        state["decision_score"] = payload.get("decision_score", signal.get("score"))
        state["score_components"] = payload.get("components", [])
        state["live_score_breakdown"] = payload
        state["component_total"] = payload.get("component_total")
        state["decision_component_total"] = payload.get("decision_component_total")
        state["enabled_weight_total"] = payload.get("enabled_weight_total")
        state["profile_weights"] = payload.get("profile_weights", signal.get("profile_weights"))
        state["profile_enabled"] = payload.get("profile_enabled", signal.get("profile_enabled"))
        state["adx_threshold"] = signal.get("adx_threshold", state.get("adx_threshold"))
        state["volume_threshold"] = signal.get("volume_threshold", state.get("volume_threshold"))
        state["strategy_profile_key"] = signal.get("strategy_profile_key", state.get("strategy_profile_key"))
        state["strategy_profile_name"] = signal.get("strategy_profile_name", state.get("strategy_profile_name"))
    except Exception as exc:
        try:
            print(f"Active strategy state display sync skipped: {str(exc)[:160]}")
        except Exception:
            pass


def apply_active_strategy_score_patch() -> None:
    if getattr(runtime, "_okai_active_strategy_score_v4", False):
        _install_live_exit_duplicate_guard()
        _install_trade_miss_audit()
        return

    # Install before wrapping _build_scan so strong trend scans do not stay blocked
    # just because ST_DIR was NEUTRAL while the numeric Supertrend line confirmed
    # the same direction.
    try:
        from bot.supertrend_neutral_line_fallback_patch import (
            apply_supertrend_neutral_line_fallback_patch,
        )

        apply_supertrend_neutral_line_fallback_patch()
    except Exception as exc:
        try:
            print(f"Supertrend line fallback patch skipped: {str(exc)[:160]}")
        except Exception:
            pass

    original_build_scan = runtime._build_scan
    original_state_update = runtime._state_update

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
            _recompute_display_payload(scan, signal, snapshot)

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

    def state_update_with_active_display(state, scans, selected, settings, rows):
        original_state_update(state, scans, selected, settings, rows)
        _sync_state_display_score(state, scans, selected, settings)

    runtime._build_scan = build_scan_with_active_profile
    runtime._state_update = state_update_with_active_display
    runtime._okai_active_strategy_score_v4 = True
    runtime._okai_active_strategy_score_v3 = True
    runtime._okai_active_strategy_score_v2 = True
    runtime._okai_active_strategy_score_v1 = True
    _install_live_exit_duplicate_guard()
    _install_trade_miss_audit()
