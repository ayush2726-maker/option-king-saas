import pandas as pd

from bot import supertrend_replay_final_patch as patch


def _frame(close=110.0, line=100.0, direction="NEUTRAL"):
    rows = []
    for index in range(30):
        rows.append(
            {
                "time": f"2026-08-06T04:{index:02d}:00+00:00",
                "open": close - 1,
                "high": close + 1,
                "low": close - 2,
                "close": close,
                "volume": 100,
                "SUPERTREND": line,
                "ST_DIR": direction,
            }
        )
    return pd.DataFrame(rows)


def _scan(candidate="CE"):
    return {
        "status": "OK",
        "candle_id": "2026-08-06T04:28:00+00:00",
        "market_data": {
            "price": 110.0,
            "vwap": 105.0,
            "ema9": 108.0,
            "ema21": 106.0,
            "trend": "UPTREND",
            "supertrend_dir": "NEUTRAL",
        },
        "signal_data": {
            "signal": "WAIT",
            "candidate_signal": candidate,
            "score": 90,
            "min_score": 82,
            "trade_allowed": False,
            "safety_gate_reasons": ["SUPERTREND_DIRECTION_REQUIRED"],
            "fresh_entry_block_reasons": [],
        },
        "chart_candles": [
            {"time": "2026-08-06T04:28:00+00:00", "score": 90}
        ],
    }


def test_completed_line_repairs_neutral_and_reopens_gate(monkeypatch):
    monkeypatch.setattr(
        patch.angel_fetcher,
        "calculate_indicators",
        lambda frame: (frame, "UPTREND"),
    )
    scan = patch._repair_replay_scan(_scan(), _frame())
    assert scan["market_data"]["supertrend_dir"] == "UP"
    assert scan["market_data"]["supertrend_value"] == 100.0
    assert "SUPERTREND_DIRECTION_REQUIRED" not in scan["signal_data"][
        "safety_gate_reasons"
    ]
    assert scan["signal_data"]["trade_allowed"] is True
    assert scan["signal_data"]["signal"] == "CE"


def test_no_numeric_line_keeps_neutral_fail_closed(monkeypatch):
    frame = _frame()
    frame["SUPERTREND"] = float("nan")
    monkeypatch.setattr(
        patch.angel_fetcher,
        "calculate_indicators",
        lambda value: (value, "SIDEWAYS"),
    )
    scan = patch._repair_replay_scan(_scan(), frame)
    assert scan["market_data"]["supertrend_dir"] == "NEUTRAL"
    assert scan["signal_data"]["trade_allowed"] is False


def test_existing_up_down_is_not_overwritten(monkeypatch):
    scan = _scan()
    scan["market_data"]["supertrend_dir"] = "DOWN"
    monkeypatch.setattr(
        patch.angel_fetcher,
        "calculate_indicators",
        lambda frame: (_frame(direction="UP"), "UPTREND"),
    )
    repaired = patch._repair_replay_scan(scan, _frame())
    assert repaired["market_data"]["supertrend_dir"] == "DOWN"
    assert repaired["market_data"]["supertrend_source"] == "REPLAY_PAYLOAD"
