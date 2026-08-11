from bot.decision_score_display_consistency_patch import (
    SCORE_MODE,
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
