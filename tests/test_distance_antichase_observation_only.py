from bot.ema_anti_chase_observation_only_patch import _repair


def _qualified_result():
    return {
        "signal": "WAIT",
        "candidate_signal": "CE",
        "score": 92,
        "min_score": 82,
        "trade_allowed": False,
        "ema_chase_blocked": True,
        "vwap_chase_blocked": True,
        "chase_blocked": True,
        "sideways_blocked": False,
        "ema_stretch_points": 30.0,
        "ema_stretch_limit": 22.0,
        "vwap_stretch_points": 48.0,
        "vwap_stretch_limit": 35.0,
        "warnings": [
            "ANTI_CHASE_EMA_STRETCH:30.0pt>22.0pt",
            "ANTI_CHASE_VWAP_STRETCH:48.0pt>35.0pt",
        ],
    }


def test_ema_and_vwap_distance_blocks_become_observation_only():
    output = _repair(_qualified_result())

    assert output["trade_allowed"] is True
    assert output["signal"] == "CE"
    assert output["ema_chase_blocked"] is False
    assert output["vwap_chase_blocked"] is False
    assert output["chase_blocked"] is False
    assert output["ema_chase_observation_only"] is True
    assert output["vwap_chase_observation_only"] is True
    assert all("ANTI_CHASE_VWAP_STRETCH" not in value for value in output["warnings"])
    assert any("VWAP_ANTI_CHASE_OBSERVATION_ONLY" in value for value in output["warnings"])


def test_distance_removal_does_not_bypass_sideways_block():
    signal = _qualified_result()
    signal["sideways_blocked"] = True

    output = _repair(signal)

    assert output["trade_allowed"] is False
    assert output["signal"] == "WAIT"
