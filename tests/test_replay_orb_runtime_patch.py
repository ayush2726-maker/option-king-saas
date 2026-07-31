from __future__ import annotations

import pandas as pd

from bot import auto_portfolio_runtime as runtime
from bot import live_scan_history_fallback_patch as replay
from bot import replay_orb_runtime_patch as patch


def _frame(include_orb=True):
    rows = []
    if include_orb:
        for minute in range(15, 31):
            price = 24400 + (minute - 15)
            rows.append(
                [
                    f"2026-07-31T09:{minute:02d}:00+05:30",
                    price,
                    price + 4,
                    price - 3,
                    price + 1,
                    0,
                ]
            )
    for minute in range(0, 35):
        price = 24450 + minute
        rows.append(
            [
                f"2026-07-31T10:{minute:02d}:00+05:30",
                price,
                price + 3,
                price - 2,
                price + 1,
                0,
            ]
        )
    return pd.DataFrame(
        rows,
        columns=["time", "open", "high", "low", "close", "volume"],
    )


def _install_over_fakes(monkeypatch, recovered_frame):
    raw_frame = _frame(include_orb=False)

    def original_collect(*args, **kwargs):
        return raw_frame.copy(), "LIVE_BROKER", ["direct_rows=35"]

    def original_replay(user_id, underlying, frame, profile, source, notes):
        return {
            "underlying": underlying,
            "status": "OK",
            "market_data": {"orb_high": 0.0, "orb_low": 0.0},
            "signal_data": {"warnings": ["REPLAY_FIRST_LIVE_SCAN"]},
        }

    def original_summary(scan):
        return {"underlying": scan.get("underlying")}

    monkeypatch.setattr(replay, "_collect_frame", original_collect)
    monkeypatch.setattr(replay, "_replay_scan", original_replay)
    monkeypatch.setattr(runtime, "_summary", original_summary)
    monkeypatch.setattr(
        replay,
        "_okai_replay_orb_runtime_v2",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        runtime,
        "_okai_replay_orb_runtime_v2",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        patch,
        "_recover_frame",
        lambda *args, **kwargs: recovered_frame.copy(),
    )

    patch.apply_replay_orb_runtime_patch()
    return raw_frame


def test_replay_runtime_recovers_and_injects_opening_range(monkeypatch):
    recovered = _frame(include_orb=True)
    _install_over_fakes(monkeypatch, recovered)

    frame, source, notes = replay._collect_frame(
        1,
        "angelone",
        object(),
        "NIFTY",
        angel=True,
    )

    high, low = patch._orb_levels(frame)
    assert high == 24419.0
    assert low == 24397.0
    assert "ORB_RECOVERED" in source
    assert any("REPLAY_ORB_RUNTIME_V2" in note for note in notes)

    result = replay._replay_scan(
        1,
        "NIFTY",
        frame,
        {"entry_threshold": 82},
        source,
        notes,
    )
    assert result["market_data"]["orb_high"] == 24419.0
    assert result["market_data"]["orb_low"] == 24397.0
    assert result["market_data"]["orb_available"] is True
    assert result["orb_runtime_patch"] == "REPLAY_ORB_RUNTIME_V2"
    assert "ORB_SESSION_RECOVERED_FOR_REPLAY" in result["signal_data"]["warnings"]

    summary = runtime._summary(result)
    assert summary["orb_high"] == 24419.0
    assert summary["orb_low"] == 24397.0
    assert summary["orb_available"] is True
    assert summary["orb_runtime_patch"] == "REPLAY_ORB_RUNTIME_V2"


def test_replay_runtime_never_invents_orb_when_broker_has_no_opening_data(monkeypatch):
    unavailable = _frame(include_orb=False)
    _install_over_fakes(monkeypatch, unavailable)

    frame, source, notes = replay._collect_frame(
        1,
        "upstox",
        object(),
        "BANKNIFTY",
        angel=False,
    )
    assert patch._orb_levels(frame) == (0.0, 0.0)
    assert "ORB_RECOVERED" not in source
    assert any("status=UNAVAILABLE" in note for note in notes)

    result = replay._replay_scan(
        1,
        "BANKNIFTY",
        frame,
        {"entry_threshold": 82},
        source,
        notes,
    )
    assert result["market_data"]["orb_high"] == 0.0
    assert result["market_data"]["orb_low"] == 0.0
    assert result["market_data"]["orb_available"] is False
    assert "ORB_SESSION_UNAVAILABLE_AFTER_RETRY" in result["signal_data"]["warnings"]
