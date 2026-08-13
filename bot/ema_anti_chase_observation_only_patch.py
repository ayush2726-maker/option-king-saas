"""Disable EMA/VWAP distance anti-chase as hard entry blockers.

EMA and VWAP stretch remain recorded as telemetry/warnings so missed-trade
learning and AI can learn whether stretched entries were good or bad. All
other independent score/safety/risk gates remain unchanged.
"""

from __future__ import annotations


def _repair(result):
    if not isinstance(result, dict):
        return result

    out = dict(result)
    ema_blocked = bool(out.get("ema_chase_blocked"))
    vwap_blocked = bool(out.get("vwap_chase_blocked"))
    if not ema_blocked and not vwap_blocked:
        return out

    # Distance stretch is now observation-only. Keep the measured values and
    # limits in the payload for UI/AI learning, but neither EMA nor VWAP may
    # veto an otherwise valid setup. Do not bypass sideways, score or any other
    # independent safety gate.
    if ema_blocked:
        out["ema_chase_observation_only"] = True
    if vwap_blocked:
        out["vwap_chase_observation_only"] = True
    out["ema_chase_blocked"] = False
    out["vwap_chase_blocked"] = False
    out["chase_blocked"] = False

    warnings = []
    for value in out.get("warnings") or []:
        text = str(value or "")
        if text in {"CUSTOM_ANTI_CHASE_EMA", "CUSTOM_ANTI_CHASE_VWAP"}:
            continue
        if text.startswith(("ANTI_CHASE_EMA_STRETCH:", "ANTI_CHASE_VWAP_STRETCH:")):
            continue
        warnings.append(value)
    if ema_blocked:
        warnings.append(
            "EMA_ANTI_CHASE_OBSERVATION_ONLY:"
            f"{float(out.get('ema_stretch_points') or 0):.1f}>"
            f"{float(out.get('ema_stretch_limit') or 0):.1f}"
        )
    if vwap_blocked:
        warnings.append(
            "VWAP_ANTI_CHASE_OBSERVATION_ONLY:"
            f"{float(out.get('vwap_stretch_points') or 0):.1f}>"
            f"{float(out.get('vwap_stretch_limit') or 0):.1f}"
        )
    out["warnings"] = list(dict.fromkeys(warnings))

    candidate = str(out.get("candidate_signal") or "WAIT").upper()
    score = int(out.get("score") or 0)
    minimum = int(out.get("min_score") or 82)
    other_block = bool(out.get("sideways_blocked"))

    if candidate in {"CE", "PE"} and score >= minimum and not other_block:
        out["trade_allowed"] = True
        out["signal"] = candidate

    return out


def apply_ema_anti_chase_observation_only_patch():
    from bot import strategy

    if getattr(strategy, "_okai_ema_anti_chase_observation_only", False):
        return True

    original_full = strategy.get_full_signal
    original_custom = strategy._custom_profile_signal

    def get_full_signal_v2(*args, **kwargs):
        return _repair(original_full(*args, **kwargs))

    def custom_profile_signal_v2(*args, **kwargs):
        return _repair(original_custom(*args, **kwargs))

    strategy.get_full_signal = get_full_signal_v2
    strategy._custom_profile_signal = custom_profile_signal_v2
    strategy._okai_ema_anti_chase_observation_only = True
    return True


__all__ = ["apply_ema_anti_chase_observation_only_patch"]
