from bot import auto_portfolio_runtime as runtime


def _candidate(name, score):
    return {
        "underlying": name,
        "status": "OK",
        "signal_data": {
            "signal": "PE",
            "trade_allowed": True,
            "score": score,
            "base_score": score,
        },
        "market_data": {"adx": 30, "volume_ratio": 0},
    }


def test_second_qualified_candidate_is_tried_when_first_is_blocked():
    candidates = [_candidate("NIFTY", 88), _candidate("SENSEX", 82)]
    state = {}
    seen = []

    def opener(candidate):
        name = candidate["underlying"]
        seen.append(name)
        state["last_entry_attempt"] = {
            "reason": "OPTION_PREMIUM_MOMENTUM_WEAK" if name == "NIFTY" else "ENTRY_OPENED",
            "stage": "FINAL_EXECUTION_GUARD",
        }
        return name == "SENSEX"

    opened = runtime._attempt_entry_candidates(candidates, opener, state)
    assert seen == ["NIFTY", "SENSEX"]
    assert opened["underlying"] == "SENSEX"
    assert state["entry_candidate_attempts"][0]["opened"] is False
    assert state["entry_candidate_attempts"][1]["opened"] is True


def test_candidate_priority_is_preserved():
    scans = [_candidate("SENSEX", 82), _candidate("NIFTY", 88)]
    ordered = runtime._eligible_candidates(scans, set())
    assert [row["underlying"] for row in ordered] == ["NIFTY", "SENSEX"]
