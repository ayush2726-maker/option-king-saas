from bot.decision_score_display_consistency_patch import (
    SCORE_MODE,
    _component_aliases,
    _normalise_scan,
)


def test_visual_strength_does_not_replace_entry_score():
    scan = {
        "underlying": "BANKNIFTY",
        "status": "OK",
        "score": 91,
        "signal_data": {
            "candidate_signal": "CE",
            "signal": "WAIT",
            "score": 72,
            "min_score": 82,
            "trade_allowed": False,
            "live_score_breakdown": {
                "score": 91,
                "display_score": 91,
                "decision_score": 72,
                "component_total": 91,
                "min_score": 82,
            },
        },
    }

    fixed = _normalise_scan(scan)
    signal = fixed["signal_data"]
    payload = signal["live_score_breakdown"]

    assert signal["score"] == 72
    assert signal["decision_score"] == 72
    assert fixed["score"] == 72
    assert payload["score"] == 72
    assert payload["decision_score"] == 72
    assert payload["display_score"] == 72
    assert payload["visual_strength_score"] == 72
    assert payload["diagnostic_visual_strength_score"] == 91
    assert payload["score_mode"] == SCORE_MODE


def test_missing_breakdown_is_left_unchanged():
    scan = {
        "status": "OK",
        "signal_data": {"score": 84, "trade_allowed": True},
    }

    assert _normalise_scan(scan) is scan
    assert scan["signal_data"]["score"] == 84


def test_component_aliases_keep_legacy_mobile_rows_in_sync():
    signal = {"base_score": 55, "adx_bonus": 0, "volume_bonus": 0, "mtf_bonus": 0}
    payload = {
        "components": [
            {"key": "vwap", "decision_score": 11},
            {"key": "supertrend", "decision_score": 11},
            {"key": "ema_trend", "decision_score": 11},
            {"key": "orb", "decision_score": 11},
            {"key": "momentum", "decision_score": 11},
            {"key": "adx", "decision_score": 20},
            {"key": "volume", "decision_score": 7},
            {"key": "mtf", "decision_score": 10},
            {"key": "availability_normalization", "decision_score": 8},
        ]
    }

    aliases = _component_aliases(payload, signal)

    assert aliases["directional_score"] == 55
    assert aliases["adx_score"] == 20
    assert aliases["volume_score"] == 7
    assert aliases["mtf_score"] == 10
    assert aliases["availability_adjustment"] == 8
