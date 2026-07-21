"""Entry Quality V2.

Fix two causes of weak one-minute entries:
1. The balanced Default 82 profile gives volume only 5 points, but the older
   missing-volume normalizer still removed 15 points before scaling. That could
   inflate a raw 75 score to 88 and qualify a setup with no ORB/momentum trigger.
2. VWAP + Supertrend + EMA describe direction, but a fresh entry also needs an
   actual trigger. Require either ORB breakout or completed two-candle momentum.

The protected score remains 82. Existing mandatory structure, reversal,
anti-chase, sideways and option-premium safeguards remain active.
"""

from bot import angel_fetcher
from bot import routes
from bot import strategy


def _number(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _profile_volume_weight(profile):
    """Return the configured volume weight; balanced default is 5, not 15."""
    if isinstance(profile, dict):
        enabled = dict(profile.get("enabled") or {})
        if not enabled.get("volume", True):
            return 0
        try:
            return max(
                0,
                min(
                    40,
                    int((profile.get("weights") or {}).get("volume", 5)),
                ),
            )
        except Exception:
            return 5
    return 5


def _trigger_state(candidate, market):
    price = _number(market.get("price"), 0)
    orb_high = _number(market.get("orb_high"), 0)
    orb_low = _number(market.get("orb_low"), 0)
    c1_bullish = bool(market.get("c1_bullish", False))
    c2_bullish = bool(market.get("c2_bullish", False))

    if candidate == "CE":
        orb = orb_high > 0 and price > orb_high + 5
        momentum = c1_bullish and c2_bullish
    elif candidate == "PE":
        orb = orb_low > 0 and price < orb_low - 5
        momentum = (not c1_bullish) and (not c2_bullish)
    else:
        orb = False
        momentum = False

    return {
        "orb": bool(orb),
        "momentum": bool(momentum),
        "passed": bool(orb or momentum),
    }


def _clean_existing_reasons(reasons, candidate):
    cleaned = []
    for reason in reasons or []:
        text = str(reason or "").strip()
        if not text:
            continue
        if text.startswith("SCORE_BELOW_"):
            continue
        if text.startswith("CORE_CONFIRMATIONS_"):
            continue
        if text == "CORE_CONFIRMATIONS_BELOW_4":
            continue
        if text == "NO_DIRECTIONAL_SIGNAL" and candidate in ("CE", "PE"):
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _apply_entry_quality_v2(signal, market, profile):
    if not isinstance(signal, dict):
        return signal

    output = dict(signal)
    candidate = str(
        output.get("candidate_signal")
        or output.get("signal")
        or "WAIT"
    ).upper()
    minimum = int(round(_number(output.get("min_score", 82), 82)))

    raw_score = int(round(_number(
        output.get("score_before_volume_normalize", output.get("score", 0)),
        0,
    )))
    current_score = int(round(_number(output.get("score", raw_score), raw_score)))

    volume_weight = _profile_volume_weight(profile)
    volume_unavailable = bool(output.get("volume_score_normalized", False))
    if volume_unavailable and volume_weight > 0:
        available_max = max(50, 100 - volume_weight)
        corrected_score = min(
            100,
            int(round(raw_score * 100.0 / available_max)),
        )
    else:
        corrected_score = current_score

    trigger = _trigger_state(candidate, market or {})
    mandatory_passed = bool(
        output.get("mandatory_confirmations_passed", False)
    )

    safety_reasons = _clean_existing_reasons(
        output.get("safety_gate_reasons") or [],
        candidate,
    )
    fresh_reasons = _clean_existing_reasons(
        output.get("fresh_entry_block_reasons") or [],
        candidate,
    )

    if candidate not in ("CE", "PE"):
        safety_reasons.append("NO_DIRECTIONAL_SIGNAL")
    if corrected_score < minimum:
        safety_reasons.append(f"SCORE_BELOW_{minimum}")
    if (
        candidate in ("CE", "PE")
        and corrected_score >= minimum
        and mandatory_passed
        and not trigger["passed"]
    ):
        safety_reasons.append("ORB_OR_MOMENTUM_TRIGGER_REQUIRED")
        fresh_reasons.append("ORB_OR_MOMENTUM_TRIGGER_REQUIRED")

    safety_reasons = list(dict.fromkeys(safety_reasons))
    fresh_reasons = list(dict.fromkeys(fresh_reasons))

    eligible = bool(
        candidate in ("CE", "PE")
        and corrected_score >= minimum
        and mandatory_passed
        and trigger["passed"]
        and not safety_reasons
    )

    warnings = list(output.get("warnings") or [])
    correction_note = (
        f"VOLUME_NORMALIZE_WEIGHT_CORRECTED:{current_score}->{corrected_score}"
    )
    if volume_unavailable and current_score != corrected_score:
        warnings.append(correction_note)
    if not trigger["passed"] and candidate in ("CE", "PE"):
        warnings.append("FRESH_TRIGGER_MISSING:ORB_OR_MOMENTUM")

    output.update({
        "score": corrected_score,
        "signal": candidate if eligible else "WAIT",
        "candidate_signal": candidate,
        "trade_allowed": eligible,
        "safety_gate_passed": eligible,
        "safety_gate_reasons": safety_reasons,
        "fresh_entry_ok": not fresh_reasons,
        "fresh_entry_block_reasons": fresh_reasons,
        "fresh_trigger_checks": {
            "orb_breakout": trigger["orb"],
            "two_candle_momentum": trigger["momentum"],
        },
        "fresh_trigger_passed": trigger["passed"],
        "fresh_trigger_required": "ORB_OR_TWO_CANDLE_MOMENTUM",
        "volume_normalization_weight": volume_weight,
        "volume_normalization_corrected": bool(volume_unavailable),
        "entry_quality_version": "ENTRY_QUALITY_V2",
        "entry_timing_mode": "MANDATORY_STRUCTURE_PLUS_FRESH_TRIGGER_V2",
        "warnings": list(dict.fromkeys(warnings)),
    })
    return output


def apply_entry_quality_v2_patch():
    if getattr(strategy, "_okai_entry_quality_v2", False):
        return

    original_signal = strategy.get_full_signal

    def quality_signal(market_data, consecutive_losses=0, profile=None):
        result = original_signal(
            market_data,
            consecutive_losses=consecutive_losses,
            profile=profile,
        )
        return _apply_entry_quality_v2(result, market_data or {}, profile)

    strategy.get_full_signal = quality_signal
    routes.get_full_signal = quality_signal
    angel_fetcher.get_full_signal = quality_signal
    strategy._okai_entry_quality_v2 = True
